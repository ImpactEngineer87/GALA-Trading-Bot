"""Atomic, conservative buying-power and stop-risk reservations for REAL entries."""
import math
import threading
import time


def _opened_before(pos, epoch):
    """True only when the position's open time is known and not after ``epoch``."""
    try:
        return float(pos['abierta_ts'].timestamp()) <= float(epoch)
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError, OSError):
        return False


def max_real_tickers(live):
    value = live.get('max_real_tickers', live.get('max_tickers', 2))
    if isinstance(value, bool) or int(value) != float(value) or int(value) < 0:
        raise ValueError('max_real_tickers must be a nonnegative integer')
    return int(value)


class RealCapacityManager:
    def __init__(self, config):
        self.config = config
        for name, minimum, default in [('notional_multiplier', 1, 1.05), ('cash_reserve_usd', 0, 0), ('max_balance_age_sec', .001, 10),
                                       ('balance_settle_sec', 0, 60)]:
            value = float(config.get(name, default))
            if not math.isfinite(value) or value < minimum:
                raise ValueError(f'Invalid capacity setting: {name}')
        self.lock = threading.RLock()
        self.reservations = {}
        self.last = {'allowed': False, 'reason': 'No broker capacity check yet'}

    def release(self, ticker):
        with self.lock:
            self.reservations.pop(ticker, None)

    def reserve(self, ticker, quantity, price, stop, balance, remaining_daily, positions):
        with self.lock:
            try:
                result = self._reserve(ticker, quantity, price, stop, balance, remaining_daily, positions)
            except (TypeError, ValueError, OverflowError):
                result = {'allowed': False, 'reason': 'Invalid or unavailable capacity inputs'}
            self.last = result
            return dict(result)

    def _reserve(self, ticker, quantity, price, stop, balance, remaining_daily, positions):
        cfg = self.config
        if balance.get('source') != 'BROKER':
            return {'allowed': False, 'reason': 'Confirmed broker balance required'}
        age = time.time() - float(balance.get('as_of_epoch', 0))
        if not 0 <= age <= float(cfg.get('max_balance_age_sec', 10)):
            return {'allowed': False, 'reason': 'Broker balance is stale'}
        funds = balance.get('available_funds')
        if funds is None:
            return {'allowed': False, 'reason': 'Available margin/funds missing from broker response'}
        values = [quantity, price, stop, remaining_daily, funds, balance.get('buying_power')]
        if not all(math.isfinite(float(x)) for x in values) or min(float(quantity), float(price), float(stop)) <= 0:
            raise ValueError('Invalid capacity input')
        if float(quantity) != int(quantity) or float(price) == float(stop):
            raise ValueError('Invalid quantity or stop distance')
        # In-flight entries and fills younger than the settle window may not be in
        # the broker balance yet, so their notional is reserved again to prevent
        # overspend when balances lag simultaneous submissions. v3.3.23: a fill
        # older than the window is already deducted by Schwab. Reserving it a
        # second time held a small account to one REAL position at a time.
        # Stop risk and the position count still apply to every position.
        settled_before = float(balance['as_of_epoch']) - float(cfg.get('balance_settle_sec', 60))
        occupied = {}
        settled = set()
        for symbol, pos in positions.items():
            if symbol == ticker or pos.get('execution_mode') != 'REAL':
                continue
            order = pos.get('orden')
            qty = pos.get('cantidad_resto', 0) if pos.get('tp1_hit') else pos.get('cantidad', getattr(order, 'cantidad', 0))
            entry = pos.get('entrada', getattr(order, 'precio_limite', 0))
            sl = pos.get('sl_actual', pos.get('sl', getattr(order, 'stop_loss', 0)))
            if qty and entry and sl:
                in_balance = (not pos.get('reservado') and not pos.get('pendiente')
                              and _opened_before(pos, settled_before))
                if in_balance:
                    settled.add(symbol)
                occupied[symbol] = {'notional': 0. if in_balance else abs(float(qty) * float(entry)),
                                    'risk': abs(float(entry) - float(sl)) * float(qty)}
            elif not pos.get('reservado'):
                return {'allowed': False, 'reason': f'Cannot price existing REAL exposure: {symbol}'}
        self.reservations = {k: v for k, v in self.reservations.items() if k in positions and k not in settled}
        occupied.update({k: v for k, v in self.reservations.items() if k != ticker})
        notional = int(quantity) * float(price) * float(cfg.get('notional_multiplier', 1.05))
        risk = int(quantity) * abs(float(price) - float(stop))
        available = max(0., min(float(balance['buying_power']), float(funds)) - float(cfg.get('cash_reserve_usd', 0)))
        available -= sum(row['notional'] for row in occupied.values())
        risk_left = max(0., float(remaining_daily)) - sum(row['risk'] for row in occupied.values())
        capital_slots = max(0, math.floor(available / notional))
        risk_slots = max(0, math.floor(risk_left / risk))
        ceiling = max(0, int(cfg.get('max_positions', 30)))
        free_slots = max(0, ceiling - len(occupied))
        allowed = min(capital_slots, risk_slots, free_slots) >= 1
        result = {'allowed': allowed, 'reason': 'Capacity reserved' if allowed else 'Insufficient buying power, daily risk, or position capacity',
                  'ticker': ticker, 'buying_power': balance['buying_power'], 'available_margin': funds,
                  'available_after_reservations': round(max(0, available), 2),
                  'remaining_daily_risk': round(max(0, risk_left), 2),
                  'requested_notional': round(notional, 2), 'requested_stop_risk': round(risk, 2),
                  'open_or_reserved_positions': len(occupied),
                  'positions_already_in_broker_balance': len(settled),
                  'additional_positions_for_this_order_size': min(capital_slots, risk_slots, free_slots),
                  'max_simultaneous_for_this_order_size': len(occupied) + min(capital_slots, risk_slots, free_slots),
                  'as_of_epoch': balance['as_of_epoch']}
        if allowed:
            self.reservations[ticker] = {'notional': notional, 'risk': risk}
        return result
