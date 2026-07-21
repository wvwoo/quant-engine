import yfinance as yf, numpy as np
from datetime import datetime
TICKERS = ['SPY','QQQ','NVDA','AAPL','TSLA']
today = datetime.now().date()
print('QTS V8 SCANNER v4:', today)
print('='*60)
for t in TICKERS:
    try:
        tk = yf.Ticker(t)
        h5 = tk.history(period='5d')
        if h5.empty:
            print(t,': NO DATA')
            continue
        p = h5['Close'].iloc[-1]
        exps = list(tk.options)
        if not exps:
            print(t,': NO OPTIONS')
            continue
        target = None
        for e in exps:
            d = (datetime.strptime(e,'%Y-%m-%d').date()-today).days
            if 25 <= d <= 60:
                target = e
                break
        if not target:
            for e in exps:
                d = (datetime.strptime(e,'%Y-%m-%d').date()-today).days
                if d >= 14:
                    target = e
                    break
        if not target:
            for e in exps:
                d = (datetime.strptime(e,'%Y-%m-%d').date()-today).days
                if d >= 7:
                    target = e
                    break
        if not target:
            target = exps[-1]
        dte = (datetime.strptime(target,'%Y-%m-%d').date()-today).days
        print('Fetching',t,'options for',target,'...')
        ch = tk.option_chain(target)
        cs = ch.calls
        ps = ch.puts
        strikes_c = cs['strike'].tolist()
        strikes_p = ps['strike'].tolist()
        atm = min(strikes_c, key=lambda x:abs(x-p))
        row_c = cs[cs['strike']==atm]
        row_p = ps[ps['strike']==atm]
        if row_c.empty:
            print(t,': NO ATM ROW')
            continue
        iv = float(row_c['impliedVolatility'].iloc[0])
        cbid = float(row_c['bid'].iloc[0])
        cask = float(row_c['ask'].iloc[0])
        cvol = float(row_c['volume'].iloc[0])
        coi = float(row_c['openInterest'].iloc[0])
        pbid = float(row_p['bid'].iloc[0]) if not row_p.empty else 0
        pask = float(row_p['ask'].iloc[0]) if not row_p.empty else 0
        hy = tk.history(period='1y')['Close']
        hv = float(hy.pct_change().std()*np.sqrt(252)) if len(hy)>20 else 0.5
        r = iv/hv if hv>0 else 1.0
        if r>1.5: s='SELL PREMIUM: Iron Condor / Credit Spread'
        elif r>1.2: s='MILD SELL: Covered Call / Cash-Secured Put'
        elif r>0.9: s='NEUTRAL: Calendar / Vertical'
        else: s='BUY PREMIUM: Debit Spread / Directional'
        spread_c = round(cask-cbid,2)
        print(t+' $'+str(round(float(p),2))+' | Exp:'+target+' | DTE:'+str(dte)+'d')
        print('  IV: '+f'{iv:.2%}'+' | HV: '+f'{hv:.2%}'+' | IV/HV: '+str(round(r,2)))
        print('  Call bid/ask: '+str(cbid)+'/'+str(cask)+' spread:'+str(spread_c)+' vol:'+str(int(cvol))+' OI:'+str(int(coi)))
        print('  Put  bid/ask: '+str(pbid)+'/'+str(pask))
        print('  Signal: '+s)
        print()
    except Exception as e:
        print(t+' ERR: '+str(e))
print('='*60)
ranked = []
print('NFA')
