"""Your ledger. This is the whole assignment.

`client.py` handles the network and hands you one event at a time. You return
the journal legs it produced. Some events correctly produce none: return an
empty list, not None-as-an-accident.

One event type is implemented as a worked example. The rest raise, with the rule
from PROTOCOL.md quoted in the message, so a practice run tells you exactly what
is left rather than silently scoring zero.

Two things to get right before anything else:

  * Use `Decimal`, never `float`. Money here does not always divide evenly, and
    a float implementation will disagree with us by a cent in places you will
    struggle to find.
  * Key balances by (customer, account), not by account. At least one event
    moves money between two customers on the same account, and an
    account-level book shows nothing wrong at all.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

D = Decimal
ZERO = D("0.00")

BROKERS = {
    "BRK-A": {
        "trades": {"equity", "etf"},
        "brokerage": D("0.0020"),
        "custody": D("0.0004"),
        "broker_cost": D("0.0009"),
        "custody_cost": D("0.0002"),
        "min_fee": D("1.00"),
        "ticket": D("0.35"),
        "payable_account": "2411"
    },
    "BRK-B": {
        "trades": {"equity", "bond"},
        "brokerage": D("0.0015"),
        "custody": D("0.0005"),
        "broker_cost": D("0.0008"),
        "custody_cost": D("0.0003"),
        "min_fee": D("2.50"),
        "ticket": D("3.00"),
        "payable_account": "2412"
    },
    "BRK-C": {
        "trades": {"etf", "bond"},
        "brokerage": D("0.0025"),
        "custody": D("0.0003"),
        "broker_cost": D("0.0012"),
        "custody_cost": D("0.0001"),
        "min_fee": D("0.50"),
        "ticket": D("0.20"),
        "payable_account": "2413"
    }
}


def money(x: Decimal) -> Decimal:
    """2 decimal places, half away from zero. Not round(), which is half-even."""
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {"account": account, "customer_id": customer_id,
            "debit": str(money(D(debit))), "credit": str(money(D(credit)))}


def format_qty(q: Decimal) -> str:
    s = f"{q:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


class Book:
    def __init__(self) -> None:
        # balances[(customer_id, account)] = debit-positive balance
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self.seen: set[str] = set()
        self.posted_accounts: set[str] = set()
        
        # State tracking structures
        self.open_orders: dict[str, dict] = {}
        self.lots: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        self.withdrawal_settings: dict[str, dict] = {}
        # What you have not written yet. An unimplemented handler must not stop
        # the run: the client keeps consuming and tells you the list at the end.

        self.fee_charged: dict[str, Decimal] = {}
        self.trades: dict[str, dict] = {}
        self.refunded_fees: set[str] = set()
        
        self.event_legs: dict[str, list[dict]] = {}
        self.event_log: list[dict] = []
        self.reversed_events: set[str] = set()
        self.history: dict[str, dict] = {}
        
        self.todo: dict[str, int] = defaultdict(int)

    def apply(self, ev: dict) -> list[dict]:
        eid = ev["event_id"]
        if eid in self.seen:
            return []
        self.seen.add(eid)

        handler = getattr(self, "on_" + ev["type"], None)
        if handler is None:
            self.todo[ev["type"]] += 1
            self.history[eid] = copy.deepcopy(self.get_current_snapshot_dict())
            return []
        try:
            legs = handler(ev["payload"], ev) or []
        except NotImplementedError:
            self.todo[ev["type"]] += 1
            self.history[eid] = copy.deepcopy(self.get_current_snapshot_dict())
            return []
        except Rejected:
            self.history[eid] = copy.deepcopy(self.get_current_snapshot_dict())
            return []
        except Exception:
            self.history[eid] = copy.deepcopy(self.get_current_snapshot_dict())
            return []

        self._post(legs)
        self.event_legs[eid] = legs
        self.event_log.append(ev)
        self.history[eid] = copy.deepcopy(self.get_current_snapshot_dict())
        return legs

    def _post(self, legs: list[dict]) -> None:
        dr = sum((D(l["debit"]) for l in legs), ZERO)
        cr = sum((D(l["credit"]) for l in legs), ZERO)
        if money(dr) != money(cr):
            raise AssertionError(f"unbalanced: dr {dr} cr {cr}")
        for l in legs:
            acct = l["account"]
            self.posted_accounts.add(acct)
            self.balances[(l["customer_id"], acct)] += (
                D(l["debit"]) - D(l["credit"]))

    # -- worked example -----------------------------------------------------
    def on_deposit(self, p: dict, ev: dict) -> list[dict]:
        amount = money(D(p["amount"]))
        if amount <= ZERO:
            raise Rejected("negative or zero amount")
        cid = p["customer_id"]
        return [leg("1100", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    # -- Cash Handlers ------------------------------------------------------
    def on_fee_charged(self, p: dict, ev: dict) -> list[dict]:
        amount = money(D(p["amount"]))
        if amount <= ZERO:
            raise Rejected("negative or zero amount")
        cid = p["customer_id"]
        self.fee_charged[ev["event_id"]] = amount
        return [leg("2010", cid, debit=amount),
                leg("1100", cid, credit=amount)]

    def on_fee_refund(self, p: dict, ev: dict) -> list[dict]:
        ref_id = p["refunds_source_id"]
        cid = p["customer_id"]
        if ref_id not in self.fee_charged:
            raise Rejected("fee_charged not found")
        if ref_id in self.refunded_fees:
            raise Rejected("fee already refunded")
        self.refunded_fees.add(ref_id)
        amount = self.fee_charged[ref_id]
        return [leg("1100", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    def on_interest_credited(self, p: dict, ev: dict) -> list[dict]:
        gross = money(D(p["gross_amount"]))
        cust_share = money(D(p["customer_share"]))
        if gross <= ZERO or cust_share <= ZERO or (gross - cust_share) < ZERO:
            raise Rejected("invalid interest amounts")
        firm_share = gross - cust_share
        cid = p["customer_id"]
        return [leg("1100", cid, debit=gross),
                leg("2010", cid, credit=cust_share),
                leg("4200", cid, credit=firm_share)]

    def on_transfer_between_customers(self, p: dict, ev: dict) -> list[dict]:
        from_cid = p["from_customer_id"]
        to_cid = p["to_customer_id"]
        amount = money(D(p["amount"]))
        if amount <= ZERO:
            raise Rejected("negative or zero amount")
        return [leg("2010", from_cid, debit=amount),
                leg("2010", to_cid, credit=amount)]

    def on_fx_deposit(self, p: dict, ev: dict) -> list[dict]:
        usd_market = money(D(p["usd_at_market_rate"]))
        usd_cust = money(D(p["usd_at_customer_rate"]))
        if usd_market <= ZERO or usd_cust <= ZERO:
            raise Rejected("negative or zero amount")
        if usd_cust > usd_market:
            raise Rejected("customer rate better than market rate")
        spread = usd_market - usd_cust
        cid = p["customer_id"]
        return [leg("1100", cid, debit=usd_market),
                leg("2010", cid, credit=usd_cust),
                leg("4100", cid, credit=spread)]

    def on_withdrawal_requested(self, p: dict, ev: dict) -> list[dict]:
        w_id = p["withdrawal_id"]
        amount = money(D(p["amount"]))
        if amount <= ZERO:
            raise Rejected("negative or zero amount")
        cid = p["customer_id"]
        self.withdrawal_settings[w_id] = {"amount": amount, "customer_id": cid, "status": "pending"}
        return [leg("2010", cid, debit=amount),
                leg("2300", cid, credit=amount)]

    def on_withdrawal_settled(self, p: dict, ev: dict) -> list[dict]:
        w_id = p["withdrawal_id"]
        if w_id not in self.withdrawal_settings:
            raise Rejected("withdrawal request not found")
        w = self.withdrawal_settings[w_id]
        if w.get("status") != "pending":
            raise Rejected("withdrawal already settled or rejected")
        w["status"] = "settled"
        amount = w["amount"]
        cid = w["customer_id"]
        return [leg("2300", cid, debit=amount),
                leg("1100", cid, credit=amount)]

    def on_withdrawal_rejected(self, p: dict, ev: dict) -> list[dict]:
        w_id = p["withdrawal_id"]
        if w_id not in self.withdrawal_settings:
            raise Rejected("withdrawal request not found")
        w = self.withdrawal_settings[w_id]
        if w.get("status") != "pending":
            raise Rejected("withdrawal already settled or rejected")
        w["status"] = "rejected"
        amount = w["amount"]
        cid = w["customer_id"]
        return [leg("2300", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    # -- Orders & Routing ----------------------------------------------------
    def route_order(self, asset_class: str, quantity: Decimal, limit_price: Decimal) -> str:
        P_est = quantity * limit_price
        best_broker = None
        best_charge = None
        
        for b_id in sorted(BROKERS.keys()):
            b_info = BROKERS[b_id]
            if asset_class in b_info["trades"]:
                brokerage_est = money(max(P_est * b_info["brokerage"], b_info["min_fee"]))
                custody_est = money(P_est * b_info["custody"])
                charge_est = brokerage_est + custody_est
                if best_charge is None or charge_est < best_charge:
                    best_charge = charge_est
                    best_broker = b_id
        return best_broker

    def on_order_placed(self, p: dict, ev: dict) -> list[dict]:
        order_id = p["order_id"]
        cid = p["customer_id"]
        side = p["side"]
        symbol = p["symbol"]
        quantity = D(p["quantity"])
        limit_price = D(p["limit_price"])
        asset_class = p["asset_class"]
        est_charges = D(p["est_charges"])
        
        if quantity <= ZERO or limit_price <= ZERO or est_charges < ZERO:
            raise Rejected("invalid order quantities or prices")
            
        broker = self.route_order(asset_class, quantity, limit_price)
        
        if side == "buy":
            initial_hold = money(quantity * limit_price + est_charges)
        else:
            initial_hold = ZERO
            
        if order_id in self.open_orders:
            o = self.open_orders[order_id]
            o["quantity"] = quantity
            o["limit_price"] = limit_price
            o["est_charges"] = est_charges
            o["initial_hold"] = initial_hold
            if o["closed"]:
                o["remaining_hold"] = ZERO
            else:
                total_filled = o.get("total_filled_qty", ZERO)
                released = money(initial_hold * total_filled / quantity)
                o["remaining_hold"] = max(initial_hold - released, ZERO)
        else:
            self.open_orders[order_id] = {
                "customer_id": cid,
                "side": side,
                "symbol": symbol,
                "quantity": quantity,
                "limit_price": limit_price,
                "est_charges": est_charges,
                "remaining_qty": quantity,
                "initial_hold": initial_hold,
                "remaining_hold": initial_hold,
                "broker": broker,
                "pre_filled_qty": ZERO,
                "total_filled_qty": ZERO,
                "closed": False
            }
        return []

    def on_order_cancelled(self, p: dict, ev: dict) -> list[dict]:
        order_id = p["order_id"]
        if order_id in self.open_orders:
            o = self.open_orders[order_id]
            o["remaining_hold"] = ZERO
            o["closed"] = True
        return []

    def on_order_rejected(self, p: dict, ev: dict) -> list[dict]:
        return self.on_order_cancelled(p, ev)

    # -- Fills & FIFO cost relief --------------------------------------------
    def on_order_partially_filled(self, p: dict, ev: dict) -> list[dict]:
        return self.on_order_filled(p, ev)

    def on_order_filled(self, p: dict, ev: dict) -> list[dict]:
        order_id = p["order_id"]
        cid = p["customer_id"]
        side = p["side"]
        symbol = p["symbol"]
        qty_fill = D(p["quantity"])
        P = D(p["principal"])
        broker = p["broker"]
        partner_rate = D(p["partner_rate"])
        trade_id = p["trade_id"]
        
        if qty_fill <= ZERO or P <= ZERO:
            raise Rejected("negative or zero qty/principal")
            
        self.trades[trade_id] = {
            "customer_id": cid,
            "side": side,
            "principal": P
        }
        
        b_info = BROKERS[broker]
        b = money(max(P * b_info["brokerage"], b_info["min_fee"]))
        c = money(P * b_info["custody"])
        r = money(P * D("0.0008"))
        bc = money(P * b_info["broker_cost"]) + b_info["ticket"]
        cc = money(P * b_info["custody_cost"])
        ps = money(partner_rate * max(b + c - bc - cc, ZERO))
        
        is_final = (ev["type"] == "order_filled")
        
        hold_released = ZERO
        if order_id not in self.open_orders:
            self.open_orders[order_id] = {
                "customer_id": cid,
                "side": side,
                "symbol": symbol,
                "quantity": ZERO,
                "limit_price": ZERO,
                "est_charges": ZERO,
                "remaining_qty": ZERO,
                "initial_hold": ZERO,
                "remaining_hold": ZERO,
                "broker": broker,
                "pre_filled_qty": ZERO,
                "total_filled_qty": ZERO,
                "closed": False
            }
            
        o = self.open_orders[order_id]
        o["total_filled_qty"] = o.get("total_filled_qty", ZERO) + qty_fill
        
        if o["side"] == "buy":
            if is_final or o["closed"]:
                hold_released = o["remaining_hold"]
                o["remaining_hold"] = ZERO
                o["closed"] = True
            else:
                if o["quantity"] > ZERO:
                    hold_released = money(o["initial_hold"] * qty_fill / o["quantity"])
                    hold_released = min(hold_released, o["remaining_hold"])
                    o["remaining_hold"] -= hold_released
                else:
                    o["pre_filled_qty"] += qty_fill
        else:
            if is_final:
                o["closed"] = True
                
        if side == "buy":
            self.lots[cid][symbol].append({
                "qty": qty_fill,
                "cost": P,
                "orig_qty": qty_fill,
                "orig_cost": P,
                "event_id": ev["event_id"]
            })
            
            pay_acct = b_info["payable_account"]
            return [
                leg("2010", cid, debit=P + b + c + r),
                leg("2350", cid, credit=P),
                leg("1200", cid, debit=P),
                leg("2100", cid, credit=P),
                leg("5000", cid, debit=bc),
                leg("4000", cid, credit=b),
                leg("5010", cid, debit=cc),
                leg("4010", cid, credit=c),
                leg("5100", cid, debit=ps),
                leg("2400", cid, credit=r),
                leg(pay_acct, cid, credit=bc),
                leg("2420", cid, credit=cc),
                leg("2430", cid, credit=ps)
            ]
        else:
            current_qty = sum(lot["qty"] for lot in self.lots[cid][symbol])
            if qty_fill > current_qty:
                raise Rejected("oversell")
                
            remaining_to_sell = qty_fill
            cost_total = ZERO
            lots_to_remove = []
            
            for idx, lot in enumerate(self.lots[cid][symbol]):
                if remaining_to_sell <= ZERO:
                    break
                if "orig_qty" not in lot:
                    lot["orig_qty"] = lot["qty"]
                if "orig_cost" not in lot:
                    lot["orig_cost"] = lot["cost"]
                    
                if lot["qty"] <= remaining_to_sell:
                    cost_total += lot["cost"]
                    remaining_to_sell -= lot["qty"]
                    lots_to_remove.append(idx)
                else:
                    cost_relieved = money(lot["orig_cost"] * remaining_to_sell / lot["orig_qty"])
                    cost_relieved = min(cost_relieved, lot["cost"])
                    cost_total += cost_relieved
                    lot["qty"] -= remaining_to_sell
                    lot["cost"] -= cost_relieved
                    remaining_to_sell = ZERO
                    
            for idx in reversed(lots_to_remove):
                self.lots[cid][symbol].pop(idx)
                
            pay_acct = b_info["payable_account"]
            wallet_delta = P - b - c - r
            wallet_leg = leg("2010", cid, credit=wallet_delta) if wallet_delta >= ZERO else leg("2010", cid, debit=-wallet_delta)
            
            return [
                leg("1150", cid, debit=P),
                leg("2100", cid, debit=cost_total),
                leg("5000", cid, debit=bc),
                leg("5010", cid, debit=cc),
                leg("5100", cid, debit=ps),
                wallet_leg,
                leg("1200", cid, credit=cost_total),
                leg("4000", cid, credit=b),
                leg("4010", cid, credit=c),
                leg("2400", cid, credit=r),
                leg(pay_acct, cid, credit=bc),
                leg("2420", cid, credit=cc),
                leg("2430", cid, credit=ps)
            ]

    def on_trade_settled(self, p: dict, ev: dict) -> list[dict]:
        trade_id = p["trade_id"]
        if trade_id not in self.trades:
            raise Rejected("trade not found")
        t = self.trades[trade_id]
        cid = t["customer_id"]
        P = t["principal"]
        if t["side"] == "buy":
            return [leg("2350", cid, debit=P),
                    leg("1100", cid, credit=P)]
        else:
            return [leg("1100", cid, debit=P),
                    leg("1150", cid, credit=P)]

    # -- Corporate Actions ---------------------------------------------------
    def on_dividend_cash(self, p: dict, ev: dict) -> list[dict]:
        cid = p["customer_id"]
        net = money(D(p["net_amount"]))
        if net <= ZERO:
            raise Rejected("negative or zero net_amount")
        return [leg("1100", cid, debit=net),
                leg("2010", cid, credit=net)]

    def on_dividend_reinvested(self, p: dict, ev: dict) -> list[dict]:
        cid = p["customer_id"]
        symbol = p["symbol"]
        net = money(D(p["net_amount"]))
        qty_reinvest = D(p["reinvest_quantity"])
        if net <= ZERO or qty_reinvest <= ZERO:
            raise Rejected("negative or zero net_amount or reinvest_quantity")
        
        self.lots[cid][symbol].append({
            "qty": qty_reinvest,
            "cost": net,
            "orig_qty": qty_reinvest,
            "orig_cost": net,
            "event_id": ev["event_id"]
        })
        
        return [leg("1200", cid, debit=net),
                leg("2100", cid, credit=net)]

    def on_stock_split(self, p: dict, ev: dict) -> list[dict]:
        cid = p["customer_id"]
        symbol = p["symbol"]
        ratio_from = D(p["ratio_from"])
        ratio_to = D(p["ratio_to"])
        if ratio_from <= ZERO or ratio_to <= ZERO:
            raise Rejected("invalid split ratio")
        
        ratio = ratio_to / ratio_from
        for lot in self.lots[cid][symbol]:
            lot["qty"] = lot["qty"] * ratio
            if "orig_qty" in lot:
                lot["orig_qty"] = lot["orig_qty"] * ratio
            
        return []

    def on_symbol_change(self, p: dict, ev: dict) -> list[dict]:
        cid = p["customer_id"]
        old_sym = p["old_symbol"]
        new_sym = p["new_symbol"]
        
        if old_sym in self.lots[cid]:
            self.lots[cid][new_sym].extend(self.lots[cid].pop(old_sym))
            
        return []

    # -- Fee Remittances & Payouts -------------------------------------------
    def on_broker_fees_settled(self, p: dict, ev: dict) -> list[dict]:
        cid = p["customer_id"]
        broker = p["broker"]
        pay_acct = BROKERS[broker]["payable_account"]
        amt = -self.balances[(cid, pay_acct)]
        if amt <= ZERO:
            raise Rejected("no broker fees outstanding")
        return [leg(pay_acct, cid, debit=amt),
                leg("1100", cid, credit=amt)]

    def on_custodian_fees_settled(self, p: dict, ev: dict) -> list[dict]:
        cid = p["customer_id"]
        pay_acct = "2420"
        amt = -self.balances[(cid, pay_acct)]
        if amt <= ZERO:
            raise Rejected("no custodian fees outstanding")
        return [leg(pay_acct, cid, debit=amt),
                leg("1100", cid, credit=amt)]

    def on_reg_fees_remitted(self, p: dict, ev: dict) -> list[dict]:
        cid = p["customer_id"]
        pay_acct = "2400"
        amt = -self.balances[(cid, pay_acct)]
        if amt <= ZERO:
            raise Rejected("no regulatory fees outstanding")
        return [leg(pay_acct, cid, debit=amt),
                leg("1100", cid, credit=amt)]

    def on_partner_payout(self, p: dict, ev: dict) -> list[dict]:
        cid = p["customer_id"]
        pay_acct = "2430"
        amt = -self.balances[(cid, pay_acct)]
        if amt <= ZERO:
            raise Rejected("no partner share outstanding")
        return [leg(pay_acct, cid, debit=amt),
                leg("1100", cid, credit=amt)]

    # -- Reversals & Replay --------------------------------------------------
    def on_reversal(self, p: dict, ev: dict) -> list[dict]:
        rev_id = p["reverses_event_id"]
        if rev_id not in self.event_legs:
            raise Rejected("unknown reversal reference")
            
        orig_legs = self.event_legs[rev_id]
        rev_legs = [
            leg(l["account"], l["customer_id"], debit=l["credit"], credit=l["debit"])
            for l in orig_legs
        ]
            
        self.reversed_events.add(rev_id)
        self.rebuild_state()  # Re-evaluates lots and active open orders from scratch
        
        return rev_legs

    def rebuild_state(self) -> None:
        # 1. Reset ALL mutable state structures
        self.balances = defaultdict(lambda: ZERO)
        self.posted_accounts = set()
        self.lots = defaultdict(lambda: defaultdict(list))
        self.open_orders = {}
        self.withdrawal_settings = {}
        self.fee_charged = {}
        self.trades = {}
        self.refunded_fees = set()
        
        # 2. Save and clear logs so we can preserve self.event_log
        old_log = list(self.event_log)
        self.event_log = []
        
        # 3. Re-apply all event legs to balances and conditionally replay handler
        for past_ev in old_log:
            eid = past_ev["event_id"]
            
            # Re-apply debit/credit balances from posted legs
            legs = self.event_legs.get(eid, [])
            for l in legs:
                acct = l["account"]
                self.posted_accounts.add(acct)
                self.balances[(l["customer_id"], acct)] += (
                    D(l["debit"]) - D(l["credit"]))
                    
            if eid in self.reversed_events or past_ev["type"] == "reversal":
                self.event_log.append(past_ev)
                continue
                
            handler = getattr(self, "on_" + past_ev["type"], None)
            if handler is not None:
                try:
                    # Re-run handler to re-populate lots, open_orders, and trades
                    handler(past_ev["payload"], past_ev)
                except Exception:
                    pass
            self.event_log.append(past_ev)

    # -- reporting ----------------------------------------------------------
    def snapshot(self, as_of_event_id: str | None = None) -> dict:
        """What a checkpoint_request wants: your whole state, right now."""
        if as_of_event_id is not None:
            return copy.deepcopy(self.history.get(as_of_event_id, {}))
        return self.get_current_snapshot_dict()

    def get_current_snapshot_dict(self) -> dict:
        tb: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for acct in self.posted_accounts:
            tb[acct] = ZERO
        for (_cid, acct), bal in self.balances.items():
            tb[acct] += bal

        customers: dict[str, dict] = {}
        for (cid, acct), bal in self.balances.items():
            c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                           "cash_hold": ZERO, "positions": {}})
            if acct == "2010":
                c["wallet_cash"] += -bal

        for o in self.open_orders.values():
            if o["side"] == "buy" and not o["closed"]:
                cid = o["customer_id"]
                c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                               "cash_hold": ZERO, "positions": {}})
                c["cash_hold"] += o["remaining_hold"]

        for cid, syms in self.lots.items():
            for sym, lot_list in syms.items():
                qty_sum = sum(lot["qty"] for lot in lot_list)
                cost_sum = sum(lot["cost"] for lot in lot_list)
                if qty_sum > ZERO or cost_sum > ZERO:
                    c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                                   "cash_hold": ZERO, "positions": {}})
                    c["positions"][sym] = {
                        "quantity": format_qty(qty_sum),
                        "cost_basis": str(money(cost_sum))
                    }

        open_order_routes = {}
        for order_id, o in self.open_orders.items():
            if not o["closed"]:
                open_order_routes[order_id] = o["broker"]

        formatted_customers = {}
        for cid, c in sorted(customers.items()):
            formatted_customers[cid] = {
                "wallet_cash": str(money(c["wallet_cash"])),
                "cash_hold": str(money(c["cash_hold"])),
                "positions": {sym: pos for sym, pos in sorted(c["positions"].items())}
            }

        return {
            "trial_balance": {a: str(money(v)) for a, v in sorted(tb.items())},
            "customers": formatted_customers,
            "open_order_routes": open_order_routes
        }


class Rejected(Exception):
    """Raise from a handler for an event you refuse to post.

    An oversell, a reversal of something you never received, a payload that
    will not parse. Rejecting one event and carrying on beats stopping: a
    server that stalls misses everything after it.
    """
