import os, json, sqlite3, secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
import requests
import pandas as pd
from flask import Flask, jsonify, render_template, request, redirect, url_for, session, flash

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(ROOT, "radar.db"))
API_BASE = os.environ.get("MASSIVE_API_BASE_URL", "https://api.massive.com").rstrip("/")
API_KEY = os.environ.get("MASSIVE_API_KEY", "")
ADMIN_PASSWORD = os.environ.get("RADAR_ADMIN_PASSWORD", "")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")))
http = requests.Session()
http.headers.update({"User-Agent": "FaisalStockRadar/1.0"})

DEFAULT_FILTERS = {"exchange": "Any", "max_price": 5, "max_market_cap_m": 300,
                   "max_float_m": 50, "min_avg_volume": 100000, "min_relative_volume": 1,
                   "month_down": True, "scan_limit": 60}

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def conn():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS watchlist(
            id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, category TEXT NOT NULL,
            note TEXT DEFAULT '', added_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(ticker, category))""")
        c.execute("""CREATE TABLE IF NOT EXISTS providers(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, provider_type TEXT NOT NULL,
            base_url TEXT DEFAULT '', categories TEXT DEFAULT '[]', enabled INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 10, adapter_status TEXT DEFAULT 'NOT_IMPLEMENTED',
            credential_env_name TEXT DEFAULT '', created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY, ticker TEXT, evaluated_at TEXT, price REAL,
            classification TEXT, quality INTEGER, risk TEXT, payload TEXT)""")
        if not c.execute("SELECT id FROM providers WHERE name='Massive'").fetchone():
            c.execute("""INSERT INTO providers(name,provider_type,base_url,categories,enabled,priority,
                adapter_status,credential_env_name,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("Massive","market_data",API_BASE,json.dumps(["daily_bars","ticker_reference"]),
                 1,1,"IMPLEMENTED","MASSIVE_API_KEY",now()))
init_db()

def api_get(path, params=None, full_url=None):
    if not API_KEY:
        raise RuntimeError("مفتاح MASSIVE_API_KEY غير مضبوط في إعدادات الخادم.")
    p = dict(params or {}); p["apiKey"] = API_KEY
    r = http.get(full_url or API_BASE + path, params=p, timeout=25)
    if r.status_code in (401,403):
        raise RuntimeError(f"رفض Massive الطلب (HTTP {r.status_code}). تحقق من المفتاح وصلاحيات الاشتراك.")
    if r.status_code == 429:
        raise RuntimeError("تم تجاوز حد الطلبات (429). انتظر ثم أعد المحاولة.")
    if not r.ok:
        raise RuntimeError(f"فشل طلب Massive (HTTP {r.status_code}).")
    data = r.json()
    if data.get("status") in ("ERROR","NOT_AUTHORIZED","NOT_FOUND"):
        raise RuntimeError(data.get("error") or data.get("message") or "أعاد Massive حالة خطأ.")
    return data

def bars_for(ticker, days=260):
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(days * 1.7))
    d = api_get(f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
                {"adjusted":"true","sort":"asc","limit":5000})
    rows = d.get("results") or []
    if not rows: raise RuntimeError("لا توجد بيانات يومية متاحة لهذا الرمز.")
    f = pd.DataFrame(rows).rename(columns={"o":"Open","h":"High","l":"Low","c":"Close","v":"Volume","t":"Timestamp"})
    f = f.sort_values("Timestamp").tail(days).reset_index(drop=True)
    return f

def ref_for(ticker):
    try:
        return api_get(f"/v3/reference/tickers/{ticker}").get("results") or {}
    except Exception:
        return {}

def rsi(s, n=14):
    d=s.diff(); up=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean()
    dn=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+up/dn.replace(0, float("nan")))

def swings(a, kind="low", w=3):
    found=[]
    for i in range(w,len(a)-w):
        win=a[i-w:i+w+1]
        if (kind=="low" and a[i]<=min(win)) or (kind=="high" and a[i]>=max(win)): found.append(i)
    return found

def analyze(ticker):
    ticker=ticker.strip().upper()
    if not ticker or len(ticker)>12 or not all(x.isalnum() or x in ".-" for x in ticker):
        raise ValueError("رمز السهم غير صالح.")
    f=bars_for(ticker)
    if len(f)<60: raise RuntimeError("البيانات التاريخية أقل من 60 جلسة.")
    ref=ref_for(ticker)
    c,h,l,v,o=(f[x] for x in ("Close","High","Low","Volume","Open"))
    price=float(c.iloc[-1]); avg=float(v.tail(20).mean())
    prior=float(v.iloc[-21:-1].mean()) if len(v)>=21 else None
    rel=float(v.iloc[-1]/prior) if prior and prior>0 else None
    base=float(c.iloc[-22]) if len(c)>=22 else None
    month=((price/base)-1)*100 if base else None
    rsi_now=float(rsi(c).iloc[-1]) if pd.notna(rsi(c).iloc[-1]) else None
    macd=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
    hist=macd-macd.ewm(span=9,adjust=False).mean()
    macd_up=len(hist)>=3 and hist.iloc[-1]>hist.iloc[-2]>hist.iloc[-3]
    lows=l.tail(120).to_numpy(); highs=h.tail(120).to_numpy()
    sups=[float(lows[i]) for i in swings(lows) if lows[i]<price]
    ress=[float(highs[i]) for i in swings(highs,"high") if highs[i]>price]
    support=max(sups) if sups else None; resistance=min(ress) if ress else None
    critical=float(l.tail(60).min())
    distance=((price-support)/price*100) if support else None
    upside=((resistance-price)/price*100) if resistance else None
    last_lows=l.tail(60).to_numpy()
    sw=[last_lows[i] for i in swings(last_lows)]
    higher=len(sw)>=2 and sw[-1]>sw[-2]
    rng10=float((h-l).tail(10).mean()); rng20=float((h-l).tail(20).mean())
    contraction=rng20>0 and rng10<rng20*.8
    no_new=float(l.tail(10).min())>=float(l.iloc[-30:-10].min())
    recent=f.tail(10); down=recent.loc[recent.Close<recent.Open,"Volume"].mean()
    up=recent.loc[recent.Close>=recent.Open,"Volume"].mean()
    down=0 if pd.isna(down) else float(down); up=0 if pd.isna(up) else float(up)
    fading=up>0 and down<up*.8; heavy=up>0 and down>up*1.5
    runner=(float(c.max()/c.min()-1)*100>=100) if float(c.min())>0 else False
    mcap=ref.get("market_cap")
    flt=ref.get("float_shares") or ref.get("share_class_shares_outstanding_float")
    checklist=[]
    def add(label,state,value,evidence): checklist.append(dict(label=label,state=state,value=value,evidence=evidence))
    add("السعر أقل من 5 دولارات","PASS" if price<5 else "FAIL",f"${price:.4f}","آخر إغلاق يومي.")
    add("القيمة السوقية أقل من 300 مليون","UNKNOWN" if mcap is None else ("PASS" if float(mcap)<300e6 else "FAIL"),f"${mcap:,.0f}" if mcap is not None else "غير متاح","مرجع السهم؛ يعتمد على دعم المزود.")
    add("Float أقل من 20M أساسي / 50M موسع","UNKNOWN" if flt is None else ("PASS" if float(flt)<50e6 else "FAIL"),f"{flt:,.0f}" if flt is not None else "غير متاح","لا نستخدم shares outstanding بديلًا عن Float.")
    add("متوسط الحجم فوق 100 ألف","PASS" if avg>100000 else "FAIL",f"{avg:,.0f}","متوسط آخر 20 جلسة.")
    add("Relative Volume فوق 1","UNKNOWN" if rel is None else ("PASS" if rel>1 else "FAIL"),f"{rel:.2f}x" if rel is not None else "غير متاح","حجم آخر جلسة / متوسط 20 جلسة سابقة.")
    add("الأداء الشهري سلبي","UNKNOWN" if month is None else ("PASS" if month<0 else "FAIL"),f"{month:.2f}%" if month is not None else "غير متاح","آخر إغلاق مقارنة بإغلاق قبل نحو 21 جلسة.")
    add("قرب الدعم ضمن 5%","UNKNOWN" if distance is None else ("PASS" if distance<=5 else "FAIL"),f"{distance:.2f}%" if distance is not None else "غير متاح","أقرب قاع تأرجح تحت السعر.")
    add("Higher Lows","PASS" if higher else "FAIL","نعم" if higher else "لا","آخر قيعان تأرجح.")
    add("انكماش النطاق السعري","PASS" if contraction else "FAIL","نعم" if contraction else "لا","متوسط نطاق آخر 10 مقابل 20 جلسة.")
    add("عدم تسجيل قاع جديد","PASS" if no_new else "FAIL","نعم" if no_new else "لا","مقارنة أدنى آخر 10 جلسات بالفترة السابقة.")
    add("تراجع ضغط البيع","PASS" if fading else "FAIL","نعم" if fading else "لا","متوسط حجم جلسات الهبوط مقابل الصعود.")
    add("MACD يتحسن","PASS" if macd_up else "FAIL","نعم" if macd_up else "لا","Histogram يرتفع 3 جلسات متتالية.")
    add("Former Runner ≥100%","PASS" if runner else "FAIL","نعم" if runner else "لا","أعلى/أدنى إغلاق ضمن التاريخ المحمل.")
    add("Reverse Split","UNKNOWN","يتطلب مصدرًا موثوقًا","غير مثبت من الشموع وحدها.")
    add("Offering / Dilution","UNKNOWN","غير متحقق","يتطلب أخبارًا أو إفصاحات.")
    add("Short Available","UNKNOWN","غير متاح","ليس هو نفسه Short Interest.")
    add("الأخبار والمحفزات","UNKNOWN","غير متحقق","لا نستنتج الأخبار من السعر.")
    passed=sum(x["state"]=="PASS" for x in checklist); unknown=sum(x["state"]=="UNKNOWN" for x in checklist)
    quality=round(passed/len(checklist)*100)
    risks=[]
    if support and price<support*.98: risks.append("كسر دعم")
    if heavy: risks.append("ضغط بيع مرتفع")
    if upside is not None and upside<8: risks.append("مقاومة قريبة")
    if avg<200000: risks.append("سيولة محدودة")
    if unknown>=4: risks.append("بيانات غير مكتملة")
    risk="عالية" if "كسر دعم" in risks or len(risks)>=3 else ("متوسطة" if risks else "منخفضة")
    bounce=bool(support and price>support and c.iloc[-1]>o.iloc[-1] and v.iloc[-1]>avg)
    if support and price<support*.98: cls="فشل أو كسر دعم"
    elif support and distance is not None and distance<=5 and bounce: cls="تأكيد ارتداد"
    elif contraction and higher and no_new: cls="ارتكاز قيد التكوّن"
    elif macd_up: cls="تحسن الزخم"
    elif support and distance is not None and distance<=8: cls="قاعدة سعرية محتملة"
    elif upside is not None and upside<2: cls="قرب مقاومة / حركة ممتدة"
    else: cls="مراقبة عامة"
    ready=cls=="تأكيد ارتداد" and not risks and unknown<4 and support is not None and resistance is not None and upside is not None and upside>=8
    if ready: cls="جاهز وفق شروط الرادار"
    result={"ticker":ticker,"name":ref.get("name",ticker),"exchange":ref.get("primary_exchange"),
      "price":round(price,4),"market_cap":mcap,"float":flt,"avg_vol20":int(avg),
      "rel_vol":round(rel,2) if rel is not None else None,"monthly_performance":round(month,2) if month is not None else None,
      "rsi14":round(rsi_now,1) if rsi_now is not None else None,"macd_rising":bool(macd_up),
      "sma20":round(float(c.rolling(20).mean().iloc[-1]),4),"sma30":round(float(c.rolling(30).mean().iloc[-1]),4),
      "sma50":round(float(c.rolling(50).mean().iloc[-1]),4),"support":round(support,4) if support else None,
      "critical_support":round(critical,4),"resistance":round(resistance,4) if resistance else None,
      "dist_support":round(distance,2) if distance is not None else None,"upside_resistance":round(upside,2) if upside is not None else None,
      "touches":sum(abs(float(x)-support)/support<=.02 for x in lows) if support else 0,
      "higher_lows":bool(higher),"contraction":bool(contraction),"no_new_low":bool(no_new),"fading_sell":bool(fading),
      "former_runner":bool(runner),"quality":quality,"risk":risk,"risks":risks,"classification":cls,
      "ready":bool(ready),"invalidation":round(support*.98,4) if support else None,"checklist":checklist,
      "bars":[{"date":str(int(row.Timestamp)),"close":round(float(row.Close),4),"volume":int(row.Volume)} for row in f.tail(180).itertuples()],
      "source":"Massive API","data_time":now()}
    with conn() as c:
        c.execute("INSERT INTO history(ticker,evaluated_at,price,classification,quality,risk,payload) VALUES(?,?,?,?,?,?,?)",
                  (ticker,now(),price,cls,quality,risk,json.dumps(result)))
    return result

def scan(limit):
    limit=max(1,min(int(limit),100))
    ref=api_get("/v3/reference/tickers",{"market":"stocks","active":"true","limit":limit,"sort":"ticker","order":"asc"})
    tickers=[x.get("ticker") for x in ref.get("results",[]) if x.get("ticker") and x.get("type") not in ("ETF","ETN")]
    found=[]; errors=[]
    for ticker in tickers:
        try:
            r=analyze(ticker)
            # Missing market cap or float stays unknown, never counted as confirmed pass.
            if r["price"]<5 and r["avg_vol20"]>100000 and r["rel_vol"] is not None and r["rel_vol"]>1 and r["monthly_performance"] is not None and r["monthly_performance"]<0:
                if r["market_cap"] is not None and float(r["market_cap"])>=300e6: continue
                if r["float"] is not None and float(r["float"])>=50e6: continue
                r["initial_status"]="مطابق جزئيًا" if r["market_cap"] is None or r["float"] is None else "مطابق للفلاتر المتاحة"
                found.append(r)
        except Exception as e: errors.append({"ticker":ticker,"error":str(e)})
    found.sort(key=lambda x:(x["initial_status"]!="مطابق للفلاتر المتاحة",x["risk"]=="عالية",-x["quality"]))
    return {"results":found,"errors":errors,"scanned":len(tickers),"timestamp":now(),
            "note":"الفحص محدود بعدد الرموز المختار وليس مسحًا كاملًا لكل السوق. الحقول غير المتاحة لا تُعتبر مطابقة مؤكدة."}

@app.route("/")
def index(): return render_template("index.html", api_configured=bool(API_KEY))
@app.route("/health")
def health(): return jsonify(ok=True,service="Faisal Stock Radar",time=now())
@app.route("/api/connection")
def connection():
    if not API_KEY: return jsonify(ok=False,message="مفتاح MASSIVE_API_KEY غير مضبوط.")
    try:
        d=api_get("/v3/reference/tickers",{"market":"stocks","active":"true","limit":1})
        return jsonify(ok=True,message="نجح طلب حقيقي من Massive.",sample_count=len(d.get("results",[])),time=now())
    except Exception as e: return jsonify(ok=False,message=str(e),time=now())
@app.route("/api/scan",methods=["POST"])
def api_scan():
    try: return jsonify(ok=True,**scan((request.get_json(silent=True) or {}).get("scan_limit",60)))
    except Exception as e: return jsonify(ok=False,error=str(e)),502
@app.route("/api/ticker/<ticker>")
def api_ticker(ticker):
    try: return jsonify(ok=True,result=analyze(ticker))
    except ValueError as e: return jsonify(ok=False,error=str(e)),400
    except Exception as e: return jsonify(ok=False,error=str(e)),502
@app.route("/api/watchlist",methods=["GET","POST","DELETE"])
def watchlist():
    if request.method=="GET":
        with conn() as c: rows=c.execute("SELECT * FROM watchlist ORDER BY updated_at DESC").fetchall()
        return jsonify(ok=True,items=[dict(x) for x in rows])
    b=request.get_json(silent=True) or {}; ticker=str(b.get("ticker","")).strip().upper(); category=b.get("category","watchlist")
    if not ticker or len(ticker)>12 or not all(ch.isalnum() or ch in ".-" for ch in ticker): return jsonify(ok=False,error="رمز غير صالح."),400
    if category not in ("watchlist","ready"): return jsonify(ok=False,error="قائمة غير صالحة."),400
    with conn() as c:
        if request.method=="POST":
            stamp=now(); c.execute("""INSERT INTO watchlist(ticker,category,note,added_at,updated_at) VALUES(?,?,?,?,?)
              ON CONFLICT(ticker,category) DO UPDATE SET note=excluded.note,updated_at=excluded.updated_at""",
              (ticker,category,str(b.get("note",""))[:1000],stamp,stamp))
            return jsonify(ok=True,message="تم الحفظ.")
        c.execute("DELETE FROM watchlist WHERE ticker=? AND category=?",(ticker,category))
    return jsonify(ok=True,message="تم الحذف.")
@app.route("/api/watchlist/<category>")
def watchlist_category(category):
    if category not in ("watchlist","ready"): return jsonify(ok=False,error="قائمة غير صالحة."),400
    with conn() as c: rows=c.execute("SELECT * FROM watchlist WHERE category=? ORDER BY updated_at DESC",(category,)).fetchall()
    return jsonify(ok=True,items=[dict(x) for x in rows])
@app.route("/api/history")
def history():
    with conn() as c: rows=c.execute("SELECT id,ticker,evaluated_at,price,classification,quality,risk FROM history ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify(ok=True,items=[dict(x) for x in rows])
def admin_required(fn):
    @wraps(fn)
    def wrapper(*a,**kw):
        if not ADMIN_PASSWORD: return "عيّن RADAR_ADMIN_PASSWORD في Render قبل استخدام إعدادات الإدارة.",503
        if not session.get("admin"): return redirect(url_for("admin_login",next=request.path))
        return fn(*a,**kw)
    return wrapper
@app.route("/admin/login",methods=["GET","POST"])
def admin_login():
    if not ADMIN_PASSWORD: return render_template("login.html",disabled=True)
    if request.method=="POST":
        if secrets.compare_digest(request.form.get("password",""),ADMIN_PASSWORD):
            session.clear(); session["admin"]=True; session.permanent=True
            app.permanent_session_lifetime=timedelta(hours=8)
            return redirect(url_for("settings"))
        flash("كلمة المرور غير صحيحة.")
    return render_template("login.html",disabled=False)
@app.route("/admin/logout",methods=["POST"])
def admin_logout(): session.clear(); return redirect(url_for("index"))
@app.route("/settings")
@admin_required
def settings():
    with conn() as c: ps=[dict(x) for x in c.execute("SELECT * FROM providers ORDER BY priority,id").fetchall()]
    for p in ps: p["categories"]=json.loads(p["categories"] or "[]")
    return render_template("settings.html",providers=ps,api_configured=bool(API_KEY),admin_password_set=bool(ADMIN_PASSWORD))
@app.route("/api/admin/providers",methods=["POST"])
@admin_required
def add_provider():
    b=request.get_json(silent=True) or {}; name=str(b.get("name","")).strip()[:100]
    typ=b.get("provider_type","market_data"); base=str(b.get("base_url","")).strip(); cats=b.get("categories",[])
    env=str(b.get("credential_env_name","")).strip()
    if not name or typ not in ("market_data","news","float","corporate_actions","short_data"): return jsonify(ok=False,error="اسم أو نوع المصدر غير صالح."),400
    if base and not base.startswith("https://"): return jsonify(ok=False,error="يجب أن يبدأ الرابط بـ HTTPS."),400
    if env and not env.replace("_","").isalnum(): return jsonify(ok=False,error="اسم متغير البيئة غير صالح."),400
    with conn() as c:
        c.execute("INSERT INTO providers(name,provider_type,base_url,categories,enabled,priority,adapter_status,credential_env_name,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (name,typ,base,json.dumps(cats if isinstance(cats,list) else []),0,10,"NOT_IMPLEMENTED",env,now()))
    return jsonify(ok=True,message="تم تسجيل المصدر كإعداد فقط. يحتاج موصلًا برمجيًا مختبرًا قبل جلب البيانات.")
@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"): return jsonify(ok=False,error="المسار غير موجود."),404
    return "الصفحة غير موجودة",404
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT","5000")),debug=False)
