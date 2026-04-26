"""
╔══════════════════════════════════════════════════════════════════════════╗
║  ZEC/USDT FUTURES AUTO TRADER                                           ║
║  EXACT same logic as scanner-1.py backtest (58% WR, 2.41x PF)         ║
║  Exchange : Binance USDM Futures Testnet (or real)                     ║
║  Signal   : 14 indicators, 1h candles, 4h trend filter                 ║
║  Entry    : Market on signal close (matches backtest exactly)           ║
║  SL/TP    : swing_low - 0.2×ATR  |  entry + 2.2×risk                  ║
║  Risk     : 2% of wallet balance per trade (auto-calculated from SL)   ║
║  Sides    : LONG + SHORT                                                ║
╚══════════════════════════════════════════════════════════════════════════╝

SETUP:
  1. Go to https://testnet.binancefuture.com
  2. Login → Avatar → API Management → Generate key
  3. Paste keys below
  4. pip install requests colorama
  5. python zec_trader.py

For real Binance futures:
  Change BASE_URL = "https://fapi.binance.com"
"""

import requests, time, hmac, hashlib, statistics, sys, math
from datetime import datetime, timezone
from urllib.parse import urlencode

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
except ImportError:
    print("pip install requests colorama"); sys.exit(1)

# ══════════════════════════════════════════════════════════════
#  YOUR KEYS — get from https://testnet.binancefuture.com
# ══════════════════════════════════════════════════════════════
API_KEY    = "1JvlnEpKXd7YEUopzdfZVYcvzVuVZNMxlC76SCB5esbrVHHofkAf2HYihU6YtRjs"
API_SECRET = "Gs503pD73f8jTkb5sEQny4NLAlehsVmfA4ObRfDhbhdw0RKRt5zlMHgN7zwiyMQ4"
# ══════════════════════════════════════════════════════════════

BASE_URL   = "https://testnet.binancefuture.com"
# BASE_URL = "https://fapi.binance.com"   # ← real trading

SYMBOL     = "ZECUSDT"
INTERVAL   = "1h"           # same as backtest
CANDLES    = 200             # same window used in backtest (WIN=60, need buffer)
LEVERAGE   = 5               # 5x — conservative
RISK_PCT   = 0.02            # 2% of wallet per trade
MIN_SCORE  = 3               # same threshold as backtest
MAX_BARS   = 40              # max candles to hold (matches backtest timeout)
LOG_FILE   = "zec_futures.log"

# How often to check for new signal (only when no position open)
# Backtest trades ~5/day on 1h = signal every few hours, check every 1h
CHECK_SECS = 60 * 60        # 1 hour — aligned to candle close

SS = requests.Session()
SS.headers.update({'X-MBX-APIKEY': API_KEY, 'User-Agent': 'ZECBot/2.0'})

# ── Logger ────────────────────────────────────────────────────────────────────

def log(msg, level='INFO'):
    ts  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    col = {
        'WIN':    Fore.GREEN  + Style.BRIGHT,
        'LOSS':   Fore.RED    + Style.BRIGHT,
        'SIGNAL': Fore.CYAN   + Style.BRIGHT,
        'RISK':   Fore.YELLOW + Style.BRIGHT,
        'WARN':   Fore.YELLOW,
        'INFO':   Fore.WHITE,
    }.get(level, Fore.WHITE)
    line = f"[{ts}][{level:6}] {msg}"
    print(col + line + Style.RESET_ALL)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

# ── API ───────────────────────────────────────────────────────────────────────

# ── Timestamp sync (fixes -1021 recvWindow error) ─────────────────────────────
# Binance rejects requests if your phone clock differs from server by >1000ms.
# We fetch server time once and calculate the offset to always send correct ts.

_TIME_OFFSET = 0  # ms offset between local clock and Binance server

def _sync_time():
    """Sync local clock with Binance server time. Call once on startup."""
    global _TIME_OFFSET
    try:
        r = SS.get(BASE_URL + '/fapi/v1/time', timeout=5)
        server_ms = r.json()['serverTime']
        local_ms  = int(time.time() * 1000)
        _TIME_OFFSET = server_ms - local_ms
        log(f"Clock synced. Offset={_TIME_OFFSET}ms (phone vs Binance server)")
    except Exception as e:
        log(f"Time sync failed: {e} — using local clock", 'WARN')

def _ts():
    """Return server-synced timestamp in milliseconds."""
    return int(time.time() * 1000) + _TIME_OFFSET

def _sign(p):
    return hmac.new(API_SECRET.encode(), urlencode(p).encode(), hashlib.sha256).hexdigest()

def _get(path, params=None, signed=False):
    p = dict(params or {})
    if signed:
        p['timestamp'] = _ts(); p['recvWindow'] = 10000; p['signature'] = _sign(p)
    try:
        r = SS.get(BASE_URL + path, params=p, timeout=12)
        return r.json()
    except Exception as e:
        log(f"GET {path}: {e}", 'WARN'); return {}

def _post(path, params):
    p = dict(params)
    p['timestamp'] = _ts(); p['recvWindow'] = 10000; p['signature'] = _sign(p)
    try:
        r = SS.post(BASE_URL + path, params=p, timeout=12)
        return r.json()
    except Exception as e:
        log(f"POST {path}: {e}", 'WARN'); return {}

def _delete(path, params):
    p = dict(params)
    p['timestamp'] = _ts(); p['recvWindow'] = 10000; p['signature'] = _sign(p)
    try:
        r = SS.delete(BASE_URL + path, params=p, timeout=12)
        return r.json()
    except Exception as e:
        log(f"DELETE {path}: {e}", 'WARN'); return {}

# ── Exchange ──────────────────────────────────────────────────────────────────

def get_balance():
    """
    Fetch real USDT wallet balance from Binance futures.
    Returns dict with wallet, available, unrealized PnL.
    """
    d = _get('/fapi/v2/balance', signed=True)
    if isinstance(d, list):
        for b in d:
            if b.get('asset') == 'USDT':
                return {
                    'wallet':     float(b.get('balance', 0)),
                    'available':  float(b.get('availableBalance', 0)),
                    'unrealized': float(b.get('crossUnPnl', 0)),
                }
    # Fallback: account endpoint
    d2 = _get('/fapi/v2/account', signed=True)
    for a in d2.get('assets', []):
        if a.get('asset') == 'USDT':
            return {
                'wallet':     float(a.get('walletBalance', 0)),
                'available':  float(a.get('availableBalance', 0)),
                'unrealized': float(a.get('unrealizedProfit', 0)),
            }
    return {'wallet': 0, 'available': 0, 'unrealized': 0}

def get_mark_price():
    d = _get('/fapi/v1/ticker/price', {'symbol': SYMBOL})
    try: return float(d['price'])
    except: return None

def get_klines_futures(interval=INTERVAL, limit=CANDLES):
    """Fetch candles from futures endpoint (not spot)."""
    d = _get('/fapi/v1/klines', {'symbol': SYMBOL, 'interval': interval, 'limit': limit})
    if not isinstance(d, list): return []
    try:
        return [[float(c[1]),float(c[2]),float(c[3]),float(c[4]),float(c[5])] for c in d]
    except: return []

def get_klines_4h(limit=60):
    d = _get('/fapi/v1/klines', {'symbol': SYMBOL, 'interval': '4h', 'limit': limit})
    if not isinstance(d, list): return []
    try:
        return [[float(c[1]),float(c[2]),float(c[3]),float(c[4]),float(c[5])] for c in d]
    except: return []

def get_position():
    d = _get('/fapi/v2/positionRisk', {'symbol': SYMBOL}, signed=True)
    if isinstance(d, list):
        for p in d:
            if p.get('symbol') == SYMBOL:
                amt = float(p.get('positionAmt', 0))
                return {
                    'qty':        amt,
                    'side':       'LONG' if amt > 0 else 'SHORT' if amt < 0 else None,
                    'entry':      float(p.get('entryPrice', 0)),
                    'unrealized': float(p.get('unRealizedProfit', 0)),
                    'liq_price':  float(p.get('liquidationPrice', 0)),
                    'mark_price': float(p.get('markPrice', 0)),
                }
    return {'qty':0,'side':None,'entry':0,'unrealized':0,'liq_price':0,'mark_price':0}

def get_open_orders():
    return _get('/fapi/v1/openOrders', {'symbol': SYMBOL}, signed=True)

def get_order(order_id):
    d = _get('/fapi/v1/order', {'symbol': SYMBOL, 'orderId': order_id}, signed=True)
    return d.get('status', 'ERROR'), d

def cancel_all():
    try:
        _delete('/fapi/v1/allOpenOrders', {'symbol': SYMBOL})
        log("All open orders cancelled")
    except: pass

def init_exchange():
    """Sync clock, set one-way mode and leverage."""
    _sync_time()   # fix -1021 timestamp error
    _post('/fapi/v1/positionSide/dual', {'dualSidePosition': 'false'})
    d = _post('/fapi/v1/leverage', {'symbol': SYMBOL, 'leverage': LEVERAGE})
    if 'leverage' in d:
        log(f"Leverage set to {d['leverage']}x on {SYMBOL}")
    else:
        log(f"Leverage setup: {d}", 'WARN')

def get_sym_info():
    d = _get('/fapi/v1/exchangeInfo')
    for s in d.get('symbols', []):
        if s['symbol'] == SYMBOL:
            fi = {f['filterType']: f for f in s['filters']}
            return {
                'step':    float(fi.get('LOT_SIZE',{}).get('stepSize', 0.001)),
                'min_qty': float(fi.get('LOT_SIZE',{}).get('minQty', 0.001)),
                'tick':    float(fi.get('PRICE_FILTER',{}).get('tickSize', 0.01)),
                'min_val': float(fi.get('MIN_NOTIONAL',{}).get('notional', 1)),
            }
    return {'step': 0.001, 'min_qty': 0.001, 'tick': 0.01, 'min_val': 1}

# ── Precision ─────────────────────────────────────────────────────────────────

def _dec(v):
    s = str(v).rstrip('0')
    return len(s.split('.')[-1]) if '.' in s else 0

def floor_step(qty, step):
    return round(math.floor(qty / step) * step, _dec(step))

def floor_tick(p, tick):
    return round(math.floor(p / tick) * tick, _dec(tick))

def ceil_tick(p, tick):
    return round(math.ceil(p / tick) * tick, _dec(tick))

# ── Order Placement ───────────────────────────────────────────────────────────

def place_market(side, qty):
    d = _post('/fapi/v1/order', {
        'symbol': SYMBOL, 'side': side,
        'type': 'MARKET', 'quantity': str(qty),
    })
    if 'orderId' in d:
        log(f"MARKET {side} {qty} ZEC  id={d['orderId']}", 'SIGNAL')
        return d
    log(f"MARKET {side} failed: {d}", 'WARN')
    return None

def place_sl_order(side, qty, stop_price, tick):
    """
    STOP_MARKET SL — triggers on mark price.
    Note: STOP_MARKET does NOT use timeInForce on real Binance futures.
    """
    sp = floor_tick(stop_price, tick) if side == 'SELL' else ceil_tick(stop_price, tick)
    d  = _post('/fapi/v1/order', {
        'symbol':      SYMBOL,
        'side':        side,
        'type':        'STOP_MARKET',
        'stopPrice':   str(sp),
        'closePosition': 'true',      # closes entire position — cleaner than qty
        'workingType': 'MARK_PRICE',  # triggers on mark price not last price
    })
    if 'orderId' in d:
        log(f"SL STOP_MARKET {side} @ ${sp}  id={d['orderId']}", 'SIGNAL')
        return d
    # Fallback: try with quantity instead of closePosition
    log(f"SL closePosition failed ({d.get('msg','?')}) — trying with qty", 'WARN')
    d2 = _post('/fapi/v1/order', {
        'symbol':      SYMBOL,
        'side':        side,
        'type':        'STOP_MARKET',
        'stopPrice':   str(sp),
        'quantity':    str(qty),
        'reduceOnly':  'true',
        'workingType': 'MARK_PRICE',
    })
    if 'orderId' in d2:
        log(f"SL (fallback) {side} @ ${sp}  id={d2['orderId']}", 'SIGNAL')
        return d2
    log(f"SL completely failed: {d2}", 'WARN')
    return None

def place_tp_order(side, qty, tp_price, tick):
    """
    TAKE_PROFIT_MARKET TP — triggers on mark price.
    Note: does NOT use timeInForce on real Binance futures.
    """
    tp = ceil_tick(tp_price, tick) if side == 'SELL' else floor_tick(tp_price, tick)
    d  = _post('/fapi/v1/order', {
        'symbol':      SYMBOL,
        'side':        side,
        'type':        'TAKE_PROFIT_MARKET',
        'stopPrice':   str(tp),
        'closePosition': 'true',      # closes entire position
        'workingType': 'MARK_PRICE',
    })
    if 'orderId' in d:
        log(f"TP TAKE_PROFIT_MARKET {side} @ ${tp}  id={d['orderId']}", 'SIGNAL')
        return d
    # Fallback: try with quantity
    log(f"TP closePosition failed ({d.get('msg','?')}) — trying with qty", 'WARN')
    d2 = _post('/fapi/v1/order', {
        'symbol':      SYMBOL,
        'side':        side,
        'type':        'TAKE_PROFIT_MARKET',
        'stopPrice':   str(tp),
        'quantity':    str(qty),
        'reduceOnly':  'true',
        'workingType': 'MARK_PRICE',
    })
    if 'orderId' in d2:
        log(f"TP (fallback) {side} @ ${tp}  id={d2['orderId']}", 'SIGNAL')
        return d2
    log(f"TP completely failed: {d2}", 'WARN')
    return None

# ── Risk Calculator ───────────────────────────────────────────────────────────

def calc_risk(wallet_usdt, entry_price, sl_price):
    """
    2% Risk Position Sizer — identical math to backtest equity simulation.

    Formula:
      risk_$    = wallet × 2%
      sl_dist   = |entry - sl|           ← in price units
      qty       = risk_$ / sl_dist       ← coins to risk exactly 2%
      notional  = qty × entry
      margin    = notional / leverage

    If SL is hit → you lose exactly 2% of wallet (before fees).
    If TP hit (2.2× risk) → you gain ~4.4% of wallet.
    """
    risk_usdt  = wallet_usdt * RISK_PCT
    sl_dist    = abs(entry_price - sl_price)
    sl_pct     = sl_dist / entry_price * 100
    if sl_dist <= 0:
        return None
    qty_raw    = risk_usdt / sl_dist
    notional   = qty_raw * entry_price
    margin     = notional / LEVERAGE
    tp_dist    = sl_dist * 2.2
    tp_gain    = risk_usdt * 2.2
    return {
        'wallet':    round(wallet_usdt, 2),
        'risk_usdt': round(risk_usdt, 2),
        'sl_dist':   round(sl_dist, 4),
        'sl_pct':    round(sl_pct, 3),
        'qty_raw':   qty_raw,
        'notional':  round(notional, 2),
        'margin':    round(margin, 2),
        'tp_dist':   round(tp_dist, 4),
        'tp_gain':   round(tp_gain, 2),
    }

def print_risk_box(calc, entry, sl, tp, direction):
    arrow = '▲ LONG ' if direction == 'LONG' else '▼ SHORT'
    dc    = Fore.GREEN if direction == 'LONG' else Fore.RED
    print()
    print(Fore.YELLOW + Style.BRIGHT + "  ┌─ RISK CALCULATOR ─────────────────────────────────┐")
    print(f"  │  Direction        {dc}{arrow}{Fore.YELLOW}                       │")
    print(f"  │  Wallet Balance   ${calc['wallet']:>10,.2f} USDT                 │")
    print(f"  │  2% Risk Amount   {Fore.RED}${calc['risk_usdt']:>10.2f} USDT{Fore.YELLOW}  ← max loss        │")
    print(f"  │  Entry Price      ${entry:>10.4f}                        │")
    print(f"  │  Stop Loss        {Fore.RED}${sl:>10.4f}{Fore.YELLOW}  ({calc['sl_pct']:.2f}% away)        │")
    print(f"  │  SL Distance      ${calc['sl_dist']:>10.4f}                        │")
    print(f"  │  Take Profit      {Fore.GREEN}${tp:>10.4f}{Fore.YELLOW}  (2.2× risk)           │")
    print(f"  │  Position Size    {Fore.CYAN}{calc['qty_raw']:>10.4f} ZEC{Fore.YELLOW}                   │")
    print(f"  │  Notional Value   ${calc['notional']:>10,.2f}                        │")
    print(f"  │  Margin Used      ${calc['margin']:>10.2f} USDT  ({LEVERAGE}x leverage)   │")
    print(f"  │  Max Loss         {Fore.RED}${calc['risk_usdt']:>10.2f} USDT{Fore.YELLOW}  (2% wallet)      │")
    print(f"  │  Max Gain         {Fore.GREEN}${calc['tp_gain']:>10.2f} USDT{Fore.YELLOW}  (4.4% wallet)    │")
    print(Fore.YELLOW + Style.BRIGHT + "  └────────────────────────────────────────────────────┘" + Style.RESET_ALL)

# ── Math Primitives (exact copy from scanner-1.py) ────────────────────────────

def SMA(v, n):
    return statistics.mean(v[-n:]) if len(v) >= n else None

def EMA(v, n):
    if len(v) < n: return None
    k, e = 2/(n+1), statistics.mean(v[:n])
    for x in v[n:]: e = x*k + e*(1-k)
    return e

def EMA_series(v, n):
    if len(v) < n: return [None]*len(v)
    k = 2/(n+1); out = [None]*(n-1); e = statistics.mean(v[:n]); out.append(e)
    for x in v[n:]: e = x*k + e*(1-k); out.append(e)
    return out

def STDEV(v, n):
    return statistics.stdev(v[-n:]) if len(v) >= n else None

def ATR(candles, n=14):
    if len(candles) < n+1: return None
    trs = []
    for i in range(1, len(candles)):
        h,l,pc = candles[i][1],candles[i][2],candles[i-1][3]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return statistics.mean(trs[-n:])

def ATR_series(candles, n=14):
    if len(candles) < n+1: return [None]*len(candles)
    trs = []
    for i in range(1, len(candles)):
        h,l,pc = candles[i][1],candles[i][2],candles[i-1][3]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    out = [None]*n
    for i in range(n, len(trs)+1):
        out.append(statistics.mean(trs[i-n:i]))
    return out

def RSI(closes, n=14):
    if len(closes) < n+1: return None
    g, l = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]; g.append(max(d,0)); l.append(max(-d,0))
    ag = statistics.mean(g[-n:]); al = statistics.mean(l[-n:])
    return 100.0 if al == 0 else 100-(100/(1+ag/al))

def RSI_series(closes, n=14):
    out = [None]*(n+1)
    for i in range(n+1, len(closes)+1):
        out.append(RSI(closes[:i], n))
    return out

def MACD(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow+sig: return None, None, None
    ef = EMA_series(closes, fast); es = EMA_series(closes, slow)
    ml = [f-s if f and s else None for f,s in zip(ef,es)]
    valid = [x for x in ml if x is not None]
    if len(valid) < sig: return None, None, None
    sl2 = EMA(valid, sig); m = ml[-1]
    hist = m-sl2 if m and sl2 else None
    return m, sl2, hist

def STOCH_RSI(closes, rsi_n=14, stoch_n=14, k=3, d=3):
    if len(closes) < rsi_n+stoch_n+k+d: return None, None
    rv = [x for x in RSI_series(closes, rsi_n) if x is not None]
    if len(rv) < stoch_n: return None, None
    raw_k = []
    for i in range(stoch_n, len(rv)+1):
        w = rv[i-stoch_n:i]; lo,hi = min(w),max(w)
        raw_k.append(0.5 if hi==lo else (rv[i-1]-lo)/(hi-lo)*100)
    if len(raw_k) < k: return None, None
    k_line = statistics.mean(raw_k[-k:])
    d_line = statistics.mean(raw_k[-(k+d):-d]) if len(raw_k) >= k+d else None
    return round(k_line,2), round(d_line,2) if d_line else None

def ADX(candles, n=14):
    if len(candles) < n*2: return None, None, None
    pdm, mdm, trl = [], [], []
    for i in range(1, len(candles)):
        h,l = candles[i][1],candles[i][2]
        ph,pl,pc = candles[i-1][1],candles[i-1][2],candles[i-1][3]
        up = h-ph; dn = pl-l
        pdm.append(up if up>dn and up>0 else 0)
        mdm.append(dn if dn>up and dn>0 else 0)
        trl.append(max(h-l, abs(h-pc), abs(l-pc)))
    def smma(arr, n):
        s = sum(arr[:n]); out = [s]
        for x in arr[n:]: s = s-s/n+x; out.append(s)
        return out
    smt=smma(trl,n); smp=smma(pdm,n); smm=smma(mdm,n)
    dip=[100*p/t if t else 0 for p,t in zip(smp,smt)]
    dim=[100*m/t if t else 0 for m,t in zip(smm,smt)]
    dx=[100*abs(p-m)/(p+m) if p+m>0 else 0 for p,m in zip(dip,dim)]
    adx=statistics.mean(dx[-n:]) if len(dx)>=n else None
    return (round(adx,1) if adx else None,
            round(dip[-1],1) if dip else None,
            round(dim[-1],1) if dim else None)

def OBV(candles):
    obv=0; s=[0]
    for i in range(1, len(candles)):
        if candles[i][3]>candles[i-1][3]: obv+=candles[i][4]
        elif candles[i][3]<candles[i-1][3]: obv-=candles[i][4]
        s.append(obv)
    return s

def VWAP(candles):
    tv=sum(c[4] for c in candles)
    if tv==0: return None
    return sum(((c[1]+c[2]+c[3])/3)*c[4] for c in candles)/tv

def SUPERTREND(candles, n=10, mult=3.0):
    if len(candles)<n+2: return None, None
    atr_s=ATR_series(candles,n)
    upper,lower,st,dir_=[],[],[],[]
    for i,c in enumerate(candles):
        mid=(c[1]+c[2])/2; a=atr_s[i]
        if a is None:
            upper.append(None);lower.append(None);st.append(None);dir_.append(None);continue
        ub=mid+mult*a; lb=mid-mult*a
        if i==0 or upper[i-1] is None:
            upper.append(ub);lower.append(lb);st.append(ub);dir_.append(-1);continue
        fub=ub if ub<upper[i-1] or candles[i-1][3]>upper[i-1] else upper[i-1]
        flb=lb if lb>lower[i-1] or candles[i-1][3]<lower[i-1] else lower[i-1]
        upper.append(fub);lower.append(flb)
        if st[i-1]==upper[i-1]:
            cur_st=flb if c[3]>fub else fub; cur_dir=1 if c[3]>fub else -1
        else:
            cur_st=fub if c[3]<flb else flb; cur_dir=-1 if c[3]<flb else 1
        st.append(cur_st);dir_.append(cur_dir)
    return st[-1],dir_[-1]

def ICHIMOKU(candles):
    if len(candles)<52: return None
    def mhl(c,n): return (max(x[1] for x in c[-n:])+min(x[2] for x in c[-n:]))/2
    tk=mhl(candles,9); kj=mhl(candles,26)
    sa=(tk+kj)/2; sb=mhl(candles,52)
    price=candles[-1][3]; cloud_top=max(sa,sb); cloud_bot=min(sa,sb)
    return {
        'above_cloud': price>cloud_top,
        'below_cloud': price<cloud_bot,
        'tk_bull':     tk>kj,
    }

def RSI_DIV(candles, closes, n=14, lb=20):
    if len(closes)<n+lb+5: return None
    rv=[]
    for i in range(n+1,len(closes)+1): rv.append(RSI(closes[:i],n))
    prices=closes[n:];
    if len(prices)<lb or len(rv)<lb: return None
    pr=prices[-lb:]; rr=rv[-lb:]
    cp=pr[-1]; cr=rr[-1]
    pli=pr.index(min(pr[:-3])); phi=pr.index(max(pr[:-3]))
    bull=(cp<pr[pli] and cr>rr[pli])
    bear=(cp>pr[phi] and cr<rr[phi])
    return 'BULL' if bull else 'BEAR' if bear else None

def CMF(candles, n=20):
    if len(candles)<n: return None
    c=candles[-n:]; mfv=[]
    for bar in c:
        h,l,cl,v=bar[1],bar[2],bar[3],bar[4]
        mfv.append(((cl-l)-(h-cl))/(h-l)*v if h!=l else 0)
    tv=sum(bar[4] for bar in c)
    return sum(mfv)/tv if tv>0 else None

def WILLIAMS_R(candles, n=14):
    if len(candles)<n: return None
    c=candles[-n:]; hh=max(x[1] for x in c); ll=min(x[2] for x in c)
    cl=candles[-1][3]
    return -50 if hh==ll else ((hh-cl)/(hh-ll))*-100

# ── 4H Trend Filter (exact from scanner-1.py) ─────────────────────────────────

def htf_trend_4h():
    """Returns 'BULL', 'BEAR', or 'NEUTRAL' based on 4h EMA20/50."""
    c4 = get_klines_4h(60)
    if len(c4) < 30: return 'NEUTRAL'
    closes = [x[3] for x in c4]
    e20 = EMA(closes, 20); e50 = EMA(closes, 50)
    if not e20 or not e50: return 'NEUTRAL'
    if e20 > e50 and closes[-1] > e20: return 'BULL'
    if e20 < e50 and closes[-1] < e20: return 'BEAR'
    return 'NEUTRAL'

# ── Signal Engine — EXACT copy of score_candles from scanner-1.py ─────────────

def score_candles(candles, use_htf=True):
    """
    14-indicator confluence scorer.
    IDENTICAL to scanner-1.py score_candles() used in the backtest.
    Returns (direction, score, confluence, reasons, ind) or None.
    """
    if len(candles) < 55: return None

    closes  = [c[3] for c in candles]
    highs   = [c[1] for c in candles]
    lows    = [c[2] for c in candles]
    volumes = [c[4] for c in candles]
    price   = closes[-1]

    lv = sv = 0
    reasons = []; ind = {}

    # 1. EMA Stack 21/50/200
    e21=EMA(closes,21); e50=EMA(closes,50)
    e200=EMA(closes,200) if len(closes)>=200 else EMA(closes,min(len(closes)-1,100))
    if e21 and e50:
        ind['EMA21']=round(e21,4); ind['EMA50']=round(e50,4)
        if e21>e50 and price>e21:   lv+=1; reasons.append('EMA stack BULL: price>EMA21>EMA50')
        elif e21<e50 and price<e21: sv+=1; reasons.append('EMA stack BEAR: price<EMA21<EMA50')
        if e200:
            ind['EMA200']=round(e200,4)
            if price>e200: lv+=1; reasons.append('Price above EMA200 — macro uptrend')
            else:          sv+=1; reasons.append('Price below EMA200 — macro downtrend')

    # 2. Bollinger Band Z-Score
    m20=SMA(closes,20); s20=STDEV(closes,20)
    if m20 and s20 and s20>0:
        z=(price-m20)/s20; ind['Z']=round(z,2)
        ind['BB_lo']=round(m20-2*s20,4); ind['BB_hi']=round(m20+2*s20,4)
        if   z<=-2.0: lv+=1; reasons.append(f'BB squeeze: Z={z:.2f}, at lower band')
        elif z>= 2.0: sv+=1; reasons.append(f'BB squeeze: Z={z:.2f}, at upper band')
        elif z<=-1.5: lv+=1; reasons.append(f'BB approach: Z={z:.2f}, nearing lower')
        elif z>= 1.5: sv+=1; reasons.append(f'BB approach: Z={z:.2f}, nearing upper')

    # 3. MACD
    ml,_,hist=MACD(closes)
    if ml is not None:
        ind['MACD_hist']=round(hist,6) if hist else None
        if hist and hist>0 and ml<0:   lv+=1; reasons.append('MACD hist+, line below 0 — early bull')
        elif hist and hist<0 and ml>0: sv+=1; reasons.append('MACD hist-, line above 0 — early bear')
        elif hist and hist>0:          lv+=1; reasons.append('MACD histogram positive')
        elif hist and hist<0:          sv+=1; reasons.append('MACD histogram negative')

    # 4. RSI (double votes at extremes)
    r14=RSI(closes,14)
    if r14:
        ind['RSI']=round(r14,1)
        if   r14<=28: lv+=2; reasons.append(f'RSI={r14:.0f} extreme oversold (+2)')
        elif r14<=35: lv+=1; reasons.append(f'RSI={r14:.0f} oversold')
        elif r14>=72: sv+=2; reasons.append(f'RSI={r14:.0f} extreme overbought (+2)')
        elif r14>=65: sv+=1; reasons.append(f'RSI={r14:.0f} overbought')

    # 5. Stochastic RSI
    stk,std2=STOCH_RSI(closes)
    if stk and std2:
        ind['StochK']=stk; ind['StochD']=std2
        if   stk<20 and std2<20 and stk>std2: lv+=1; reasons.append(f'StochRSI K={stk} crossing up from OS')
        elif stk>80 and std2>80 and stk<std2: sv+=1; reasons.append(f'StochRSI K={stk} crossing down from OB')
        elif stk<25: lv+=1; reasons.append(f'StochRSI K={stk} oversold zone')
        elif stk>75: sv+=1; reasons.append(f'StochRSI K={stk} overbought zone')

    # 6. ADX + DI
    adx_v,di_p,di_m=ADX(candles,14)
    if adx_v:
        ind['ADX']=adx_v; ind['DI+']=di_p; ind['DI-']=di_m
        if adx_v>=25:
            if   di_p and di_m and di_p>di_m: lv+=1; reasons.append(f'ADX={adx_v:.0f} strong + DI+ dominant')
            elif di_p and di_m and di_m>di_p: sv+=1; reasons.append(f'ADX={adx_v:.0f} strong + DI- dominant')
        elif adx_v<20:
            reasons.append(f'ADX={adx_v:.0f} weak trend — range market')

    # 7. Supertrend
    st_v,st_d=SUPERTREND(candles,10,3.0)
    if st_d:
        ind['ST']='BULL' if st_d==1 else 'BEAR'
        if   st_d== 1: lv+=1; reasons.append('Supertrend BULLISH')
        elif st_d==-1: sv+=1; reasons.append('Supertrend BEARISH')

    # 8. Ichimoku
    ichi=ICHIMOKU(candles)
    if ichi:
        ind['Ichi']='above' if ichi['above_cloud'] else 'below' if ichi['below_cloud'] else 'in'
        if   ichi['above_cloud'] and ichi['tk_bull']:  lv+=1; reasons.append('Ichimoku: above cloud + TK bull')
        elif ichi['below_cloud'] and not ichi['tk_bull']: sv+=1; reasons.append('Ichimoku: below cloud + TK bear')

    # 9. RSI Divergence (+2 votes)
    div=RSI_DIV(candles,closes)
    if div:
        ind['RSI_Div']=div
        if   div=='BULL': lv+=2; reasons.append('RSI BULLISH DIVERGENCE — price LL, RSI HL (+2)')
        elif div=='BEAR': sv+=2; reasons.append('RSI BEARISH DIVERGENCE — price HH, RSI LH (+2)')

    # 10. CMF
    cmf=CMF(candles,20)
    if cmf is not None:
        ind['CMF']=round(cmf,3)
        if   cmf> 0.1: lv+=1; reasons.append(f'CMF={cmf:.2f} buying pressure')
        elif cmf<-0.1: sv+=1; reasons.append(f'CMF={cmf:.2f} selling pressure')

    # 11. Williams %R
    wr=WILLIAMS_R(candles,14)
    if wr is not None:
        ind['WR']=round(wr,1)
        if   wr<=-80: lv+=1; reasons.append(f'Williams %R={wr:.0f} heavily oversold')
        elif wr>=-20: sv+=1; reasons.append(f'Williams %R={wr:.0f} heavily overbought')

    # 12. OBV slope
    obv=OBV(candles)
    if len(obv)>=5:
        slope=(obv[-1]-obv[-5])/max(abs(obv[-5]),1)*100
        ind['OBV_slope']=round(slope,2)
        if   slope> 2: lv+=1; reasons.append('OBV rising — accumulation')
        elif slope<-2: sv+=1; reasons.append('OBV falling — distribution')

    # 13. VWAP
    vw=VWAP(candles)
    if vw:
        dev=((price-vw)/vw)*100; ind['VWAP_dev']=round(dev,2)
        if   dev<=-2: lv+=1; reasons.append(f'Price {dev:.1f}% below VWAP')
        elif dev>= 2: sv+=1; reasons.append(f'Price {dev:.1f}% above VWAP')

    # 14. 4H Trend Filter
    htf = 'NEUTRAL'
    if use_htf:
        htf = htf_trend_4h()
    ind['4H'] = htf

    score     = lv + sv
    direction = 'LONG' if lv > sv else 'SHORT' if sv > lv else None
    if direction is None: return None

    # HTF alignment bonus/penalty (same as scanner-1.py)
    if htf != 'NEUTRAL':
        if (direction=='LONG' and htf=='BEAR') or (direction=='SHORT' and htf=='BULL'):
            reasons.append(f'⚠ Counter-trend: 4H={htf} — score -2')
            score = max(0, score-2)
        else:
            reasons.append(f'4H trend ALIGNS: {htf} — score +1')
            score += 1

    confluence = lv if direction=='LONG' else sv
    return direction, score, confluence, reasons, ind


def build_signal(candles, use_htf=True):
    """
    Generates trading signal with SL/TP.
    SL/TP math IDENTICAL to scanner-1.py backtest:
      entry  = close price of signal candle (market order)
      SL     = swing_low[-30] - 0.2×ATR   (LONG)
             = swing_high[-30] + 0.2×ATR  (SHORT)
      TP     = entry + 2.2×risk           (LONG)
             = entry - 2.2×risk           (SHORT)
    """
    result = score_candles(candles, use_htf)
    if result is None: return None

    direction, score, confluence, reasons, ind = result
    if score < MIN_SCORE: return None

    closes = [c[3] for c in candles]
    highs  = [c[1] for c in candles]
    lows   = [c[2] for c in candles]
    price  = closes[-1]   # entry = current close (market order)

    atr_v = ATR(candles, 14)
    if not atr_v: return None

    # Swing levels — last 30 candles (same as backtest)
    swing_high = max(highs[-30:])
    swing_low  = min(lows[-30:])

    if direction == 'LONG':
        entry = price
        sl    = round(swing_low  - 0.2 * atr_v, 4)
        risk  = entry - sl
        if risk <= 0: return None
        tp    = round(entry + 2.2 * risk, 4)
    else:
        entry = price
        sl    = round(swing_high + 0.2 * atr_v, 4)
        risk  = sl - entry
        if risk <= 0: return None
        tp    = round(entry - 2.2 * risk, 4)

    return {
        'direction':  direction,
        'score':      score,
        'confluence': confluence,
        'price':      price,
        'entry':      entry,      # same as price — market order
        'sl':         sl,
        'tp':         tp,
        'rr':         round(abs(tp-entry)/abs(entry-sl), 2),
        'sl_pct':     round(abs(entry-sl)/entry*100, 3),
        'atr':        round(atr_v, 4),
        'reasons':    reasons,
        'ind':        ind,
    }

# ── Trade State ───────────────────────────────────────────────────────────────

class ST:
    active    = False
    direction = None
    entry     = None
    sl        = None
    tp        = None
    qty       = None
    sl_id     = None
    tp_id     = None
    open_bar  = 0    # track bars held (matches MAX_BARS backtest timeout)

# ── Dashboard ─────────────────────────────────────────────────────────────────

def fp(p):
    if p is None:   return '—'
    if p >= 10000:  return f'${p:,.1f}'
    if p >= 100:    return f'${p:,.2f}'
    if p >= 1:      return f'${p:,.4f}'
    return               f'${p:,.6f}'

def print_dashboard(bal, price, pos, signal=None):
    W  = 66
    uc = Fore.GREEN if bal['unrealized'] >= 0 else Fore.RED
    print()
    print(Fore.CYAN + Style.BRIGHT + '═'*W)
    print(f"  ZEC/USDT FUTURES  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  {LEVERAGE}x")
    print(Fore.CYAN + '─'*W)
    print(f"  {'Wallet Balance':<22} {Fore.GREEN}${bal['wallet']:,.2f} USDT{Style.RESET_ALL}")
    print(f"  {'Available Margin':<22} ${bal['available']:,.2f} USDT")
    print(f"  {'Unrealized PnL':<22} {uc}${bal['unrealized']:+.4f} USDT{Style.RESET_ALL}")
    print(f"  {'2% Risk/Trade':<22} {Fore.YELLOW}${bal['wallet']*RISK_PCT:.2f} USDT{Style.RESET_ALL}")
    print(f"  {'ZEC Mark Price':<22} {Fore.WHITE}${price:.4f}{Style.RESET_ALL}")
    print(Fore.CYAN + '─'*W)
    if pos['side']:
        pc = Fore.GREEN if pos['unrealized'] >= 0 else Fore.RED
        dc = Fore.GREEN if pos['side']=='LONG' else Fore.RED
        print(f"  {'OPEN POSITION':<22} {dc}{pos['side']}{Style.RESET_ALL}  qty={abs(pos['qty']):.4f}")
        print(f"  {'Entry Price':<22} ${pos['entry']:.4f}")
        print(f"  {'Mark Price':<22} ${pos['mark_price']:.4f}")
        print(f"  {'Liq Price':<22} {Fore.RED}${pos['liq_price']:.4f}{Style.RESET_ALL}")
        print(f"  {'Unrealized PnL':<22} {pc}${pos['unrealized']:+.4f} USDT{Style.RESET_ALL}")
        if ST.sl: print(f"  {'Stop Loss':<22} {Fore.RED}${ST.sl:.4f}{Style.RESET_ALL}")
        if ST.tp: print(f"  {'Take Profit':<22} {Fore.GREEN}${ST.tp:.4f}{Style.RESET_ALL}  (RR 1:{ST.tp/ST.entry:.2f} approx)")
        bars_held = ST.open_bar
        print(f"  {'Bars Held':<22} {bars_held}/{MAX_BARS}  (timeout at {MAX_BARS}h)")
    else:
        print(f"  {Style.DIM}No open position — scanning...{Style.RESET_ALL}")
    if signal:
        dc = Fore.GREEN if signal['direction']=='LONG' else Fore.RED
        arrow = '▲' if signal['direction']=='LONG' else '▼'
        print(Fore.CYAN + '─'*W)
        print(f"  {Fore.YELLOW}NEW SIGNAL{Style.RESET_ALL}  {dc}{arrow} {signal['direction']}{Style.RESET_ALL}"
              f"  Score:{signal['score']}  Confluence:{signal['confluence']}")
        print(f"  {'Entry (Market)':<22} ${signal['entry']:.4f}")
        print(f"  {'Stop Loss':<22} {Fore.RED}${signal['sl']:.4f}{Style.RESET_ALL}  ({signal['sl_pct']:.2f}% away)")
        print(f"  {'Take Profit':<22} {Fore.GREEN}${signal['tp']:.4f}{Style.RESET_ALL}  (RR 1:{signal['rr']})")
        print(f"  {'4H Trend':<22} {signal['ind'].get('4H','?')}")
        for r in signal['reasons']:
            if '⚠' in r: print(f"    {Fore.YELLOW}{r}{Style.RESET_ALL}")
            else:         print(f"    {Fore.CYAN}• {r}{Style.RESET_ALL}")
    print(Fore.CYAN + Style.BRIGHT + '═'*W)

# ── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    log('='*66)
    log(f'ZEC/USDT FUTURES BOT — {BASE_URL}')
    log(f'TF:{INTERVAL}  Score≥{MIN_SCORE}  Leverage:{LEVERAGE}x  Risk:{RISK_PCT*100:.0f}%/trade')
    log(f'SL: swing_low - 0.2×ATR  |  TP: entry + 2.2×risk  (matches backtest)')
    log(f'Max hold: {MAX_BARS} bars = {MAX_BARS}h  |  Scan: every {CHECK_SECS//60}min')
    log('='*66)

    if 'PASTE' in API_KEY:
        print(Fore.RED + Style.BRIGHT + """
  ╔══════════════════════════════════════════════════════╗
  ║  API KEYS NOT SET                                    ║
  ║                                                      ║
  ║  1. Go to https://testnet.binancefuture.com          ║
  ║  2. Login → Avatar top-right → API Management        ║
  ║  3. Generate new API key                             ║
  ║  4. Paste API_KEY and API_SECRET at top of this file ║
  ╚══════════════════════════════════════════════════════╝
""" + Style.RESET_ALL)
        sys.exit(1)

    init_exchange()
    info = get_sym_info()
    log(f"Pair info: step={info['step']}  tick={info['tick']}  minVal=${info['min_val']}")

    bars_since_open = 0
    loop_count      = 0   # used to resync clock every 12 loops (~12h)

    while True:
        try:
            loop_count += 1
            # Resync clock every 12 loops to prevent -1021 drift
            if loop_count % 12 == 0:
                _sync_time()
            price = get_mark_price()
            if not price:
                log("Cannot fetch price — retry 60s", 'WARN')
                time.sleep(60); continue

            bal = get_balance()
            pos = get_position()

            # ── Monitor open position ─────────────────────────────────────────
            if ST.active or pos['side']:
                bars_since_open += 1
                ST.open_bar = bars_since_open
                print_dashboard(bal, price, pos)

                # Check if position closed by TP or SL
                if pos['side'] is None and ST.active:
                    pnl = ((price-ST.entry)/ST.entry*100 if ST.direction=='LONG'
                           else (ST.entry-price)/ST.entry*100)
                    outcome = 'WIN' if pnl > 0 else 'LOSS'
                    pnl_usdt = bal['wallet'] * RISK_PCT * (2.2 if pnl>0 else -1)
                    log(f"CLOSED: {outcome}  Exit≈${price:.4f}  "
                        f"PnL%:{pnl:+.2f}%  PnL$:{pnl_usdt:+.2f}  "
                        f"Bars held:{bars_since_open}", outcome)
                    cancel_all()
                    ST.active=False; ST.sl_id=None; ST.tp_id=None
                    bars_since_open=0
                    time.sleep(60); continue

                # MAX_BARS timeout — force close (matches backtest behaviour)
                if bars_since_open >= MAX_BARS and pos['side']:
                    log(f"MAX_BARS ({MAX_BARS}h) timeout — closing position at market", 'WARN')
                    close_side = 'SELL' if pos['side']=='LONG' else 'BUY'
                    place_market(close_side, abs(pos['qty']))
                    cancel_all()
                    ST.active=False; ST.sl_id=None; ST.tp_id=None
                    bars_since_open=0
                    time.sleep(60); continue

                log(f"Position open: {pos['side']}  entry=${ST.entry:.4f}  "
                    f"mark=${price:.4f}  PnL=${pos['unrealized']:+.4f}  "
                    f"bar {bars_since_open}/{MAX_BARS}")
                # Check again after 1 hour (next candle close)
                time.sleep(CHECK_SECS); continue

            # ── No position — fetch candles and check signal ───────────────────
            log("No position — fetching candles and running signal engine...")
            candles = get_klines_futures()
            if len(candles) < 60:
                log("Not enough candles", 'WARN'); time.sleep(300); continue

            signal = build_signal(candles, use_htf=True)
            print_dashboard(bal, price, pos, signal)

            if signal is None:
                log(f"No signal (score<{MIN_SCORE}). Next check in {CHECK_SECS//60}min.")
                time.sleep(CHECK_SECS); continue

            # ── Risk Calculator ───────────────────────────────────────────────
            wallet = bal['wallet']
            if wallet <= 0:
                log("Wallet balance zero", 'WARN'); time.sleep(300); continue

            calc = calc_risk(wallet, signal['entry'], signal['sl'])
            if not calc:
                log("Invalid SL distance", 'WARN'); time.sleep(60); continue

            print_risk_box(calc, signal['entry'], signal['sl'], signal['tp'], signal['direction'])

            qty = floor_step(calc['qty_raw'], info['step'])
            log(f"Qty after rounding: {qty} (raw={calc['qty_raw']:.4f}  step={info['step']})")

            if qty < info['min_qty']:
                log(f"Qty {qty} < min {info['min_qty']} — need larger balance", 'WARN')
                time.sleep(CHECK_SECS); continue
            if qty * signal['entry'] < info['min_val']:
                log(f"Notional ${qty*signal['entry']:.2f} < min ${info['min_val']}", 'WARN')
                time.sleep(CHECK_SECS); continue

            log(f"ENTERING {signal['direction']}  score={signal['score']}  "
                f"entry≈${signal['entry']:.4f}  SL=${signal['sl']:.4f}  TP=${signal['tp']:.4f}  "
                f"qty={qty}  risk=${calc['risk_usdt']:.2f}  margin=${calc['margin']:.2f}", 'SIGNAL')

            # ── Place market entry (matches backtest: entry at candle close) ──
            entry_side = 'BUY' if signal['direction']=='LONG' else 'SELL'
            order = place_market(entry_side, qty)

            if not order:
                log("Market order failed", 'WARN'); time.sleep(60); continue

            # Get actual fill price
            time.sleep(1)  # brief wait for fill
            fill_price = float(order.get('avgPrice', signal['entry']))
            if fill_price == 0:
                fill_price = signal['entry']
            log(f"Filled @ ${fill_price:.4f}", 'SIGNAL')

            # ── Place SL and TP immediately ───────────────────────────────────
            close_side = 'SELL' if signal['direction']=='LONG' else 'BUY'
            sl_o = place_sl_order(close_side, qty, signal['sl'], info['tick'])
            tp_o = place_tp_order(close_side, qty, signal['tp'], info['tick'])

            if sl_o: ST.sl_id = sl_o['orderId']
            if tp_o: ST.tp_id = tp_o['orderId']

            if not sl_o:
                log("SL order failed — CLOSE MANUALLY IF NEEDED", 'WARN')

            # Update state
            ST.active    = True
            ST.direction = signal['direction']
            ST.entry     = fill_price
            ST.sl        = signal['sl']
            ST.tp        = signal['tp']
            ST.qty       = qty
            bars_since_open = 0
            ST.open_bar  = 0

            log(f"Trade open: {ST.direction}  fill=${ST.entry:.4f}  "
                f"SL=${ST.sl:.4f}  TP=${ST.tp:.4f}  qty={ST.qty}")

        except KeyboardInterrupt:
            log("Bot stopped by user — cancelling all orders")
            cancel_all(); sys.exit(0)
        except Exception as e:
            import traceback
            log(f"Loop error: {e}", 'WARN')
            traceback.print_exc()
            time.sleep(60)

if __name__ == '__main__':
    run()
