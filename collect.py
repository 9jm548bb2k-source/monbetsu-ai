# GitHub Actions用。Google Driveは使用しない。
import requests, pandas as pd, re, json, time, random, traceback, os
from pathlib import Path
from datetime import date,timedelta
from bs4 import BeautifulSoup
from urllib.parse import urljoin,parse_qs,urlparse

START_DATE='2023-01-01'
END_DATE=os.environ.get('END_DATE',date.today().isoformat())
MAX_RUN_MINUTES=25
WAIT_SECONDS=1.8
ROOT=Path(__file__).resolve().parent
SESSION=requests.Session()
SESSION.headers.update({'User-Agent':'Mozilla/5.0 (compatible; ResearchCollector/1.0)'})
BASE='https://www.hokkaidokeiba.net/raceinfo/'
def get_soup(url):
    last=None
    for i in range(3):
        try:
            r=SESSION.get(url,timeout=30);r.raise_for_status()
            time.sleep(WAIT_SECONDS+random.uniform(0,.4))
            return BeautifulSoup(r.content,'html.parser')
        except Exception as e:
            last=e;time.sleep(2*(i+1))
    raise RuntimeError(f'通信失敗 {url}: {last}')

def txt(cell):return cell.get_text(' ',strip=True).replace('\u3000',' ')

def expand_table(table):
    # HTML rowspan / colspanを正しく展開する
    pending={};out=[]
    for tr in table.select('tr'):
        row=[]; col=0
        def fill():
            nonlocal col
            while col in pending:
                val,left=pending[col];row.append(val)
                if left<=1:del pending[col]
                else:pending[col]=(val,left-1)
                col+=1
        fill()
        for td in tr.find_all(['td','th'],recursive=False):
            fill(); val=txt(td);cs=int(td.get('colspan',1));rs=int(td.get('rowspan',1))
            for j in range(cs):
                row.append(val)
                if rs>1:pending[col]=(val,rs-1)
                col+=1
            fill()
        fill()
        if row:out.append(row)
    return out

def race_links(day):
    d=day.strftime('%Y%m%d')
    soup=get_soup(BASE+f'index.php?p_day={d}')
    nums=set()
    for a in soup.find_all('a',href=True):
        u=urljoin(BASE,a['href']);q=parse_qs(urlparse(u).query)
        if 'seiseki.php' in u and q.get('p_day',[''])[0]==d:
            try:nums.add(int(q['p_rno'][0]))
            except (KeyError,ValueError):pass
    return sorted(nums)

def race_tables(day,num):
    q=f'p_day={day.strftime("%Y%m%d")}&p_rno={num:03d}'
    a=get_soup(BASE+'seiseki.php?'+q)
    o=get_soup(BASE+'odds/tanhuku.php?'+q)
    def choose(s,words):
        for table in s.select('table'):
            header=' '.join(txt(x) for x in table.select('th'))
            if all(w in header for w in words):return expand_table(table)
        raise ValueError(f'表が見つからない: {words}')
    return choose(a,['着順','馬番','馬名']),choose(o,['馬番','単勝オッズ']),a

def normalized_header(s):return re.sub(r'\s+','',s)
def table_records(rows):
    h=[normalized_header(x) for x in rows[0]]
    # 見出し「負担」「重量」が分割されているHTMLもある
    if '負担' in h and '重量' in h:
        idx=h.index('負担');h[idx]='斤量';h.pop(h.index('重量'))
    result=[]
    for i,row in enumerate(rows[1:],1):
        if len(row)!=len(h):
            # ヘッダが負担重量を別セルに持つ場合などは列名と実列数を診断に残す
            raise ValueError(f'行列不一致 row={i} header={h} row={row}')
        result.append(dict(zip(h,row)))
    return result

def parse_race(day,num):
    results,odds,soup=race_tables(day,num)
    rr=table_records(results);oo=table_records(odds)
    def horse_no(x):
        try:return int(str(x).strip())
        except:return None
    od={horse_no(x.get('馬番')):x for x in oo}
    if not rr:raise ValueError('結果0行')
    if len(od)<len(rr)-2:raise ValueError(f'オッズ行不足 結果{len(rr)} オッズ{len(od)}')
    output=[]
    for x in rr:
        no=horse_no(x.get('馬番'))
        if no is None:continue
        z=od.get(no,{})
        def numeric(v):
            try:return float(str(v).replace(',',''))
            except:return None
        def finish(v):
            try:return int(v)
            except:return None
        output.append({'開催日':day.isoformat(),'レース':num,'馬番':no,'馬名':x.get('馬名'),
          '着順':finish(x.get('着順')),'騎手':x.get('騎手'),'調教師':x.get('調教師'),
          '枠番':x.get('枠番'),'性齢':x.get('性齢'),'斤量':x.get('斤量',x.get('負担重量')),
          '馬体重':x.get('馬体重'),'走破タイム':x.get('タイム'),'着差':x.get('着差'),
          '単勝オッズ':numeric(z.get('単勝オッズ')),'複勝オッズ範囲':z.get('複勝オッズ')})
    if not output:raise ValueError('馬番の解析失敗')
    return output

DATA=ROOT/'results.csv'
LOG=ROOT/'v6_audit.csv'
CURSOR=ROOT/'v6_cursor.json'
if DATA.exists():
    old=pd.read_csv(DATA,encoding='utf-8-sig',low_memory=False)
else:
    old=pd.DataFrame(columns=['開催日','レース','馬番'])
all_rows=old.to_dict('records')
completed=set()
# 既存CSVのレースを既取得扱い。エラーのあるレースは監査に従って再試行。
for row in all_rows:
    try:completed.add((str(row['開催日'])[:10],int(row['レース'])))
    except (KeyError,ValueError,TypeError):pass
if LOG.exists():
    audit=pd.read_csv(LOG,dtype=str).fillna('')
else:
    audit=pd.DataFrame(columns=['date','race','status','count','message'])
for r in audit.itertuples(index=False):
    try:
        key=(str(r.date),int(r.race))
        if r.status=='ok':completed.add(key)
        elif r.status=='error':completed.discard(key)
    except (ValueError,AttributeError):pass
start=date.fromisoformat(START_DATE)
end=date.fromisoformat(END_DATE)
try:
    cursor=date.fromisoformat(json.loads(CURSOR.read_text())['next_date'])
except (FileNotFoundError,KeyError,ValueError,json.JSONDecodeError):
    cursor=start
if cursor<start:cursor=start
start_clock=time.monotonic()
cutoff=MAX_RUN_MINUTES*60
new_races=0; checked_days=0; issues=[]

def atomic_csv(frame,path):
    tmp=path.with_name(path.name+'.tmp')
    frame.to_csv(tmp,index=False,encoding='utf-8-sig')
    os.replace(tmp,path)

def save_checkpoint(next_day):
    global all_rows
    df=pd.DataFrame(all_rows)
    if not df.empty:
        df=df.drop_duplicates(['開催日','レース','馬番'],keep='last')
        atomic_csv(df,DATA)
        all_rows=df.to_dict('records')
    atomic_csv(audit,LOG)
    tmp=CURSOR.with_suffix('.json.tmp')
    tmp.write_text(json.dumps({'next_date':next_day.isoformat()},ensure_ascii=False))
    os.replace(tmp,CURSOR)

print('既存行数:',len(all_rows),'開始日:',cursor,'終了日:',end,flush=True)
day=cursor
while day<=end:
    if time.monotonic()-start_clock>=cutoff:break
    checked_days+=1
    try:nums=race_links(day)
    except Exception as exc:
        issues.append((str(day),'一覧',repr(exc)))
        save_checkpoint(day)
        break
    remaining=[r for r in nums if (day.isoformat(),r) not in completed]
    failed=False
    if remaining:print('開催',day,'未取得',len(remaining),flush=True)
    for race in remaining:
        if time.monotonic()-start_clock>=cutoff:
            failed=True;break
        try:
            rows=parse_race(day,race)
            all_rows=[x for x in all_rows if not (str(x.get('開催日'))==day.isoformat() and str(x.get('レース'))==str(race))]
            all_rows.extend(rows)
            audit.loc[len(audit)]=[day.isoformat(),str(race),'ok',str(len(rows)),'']
            completed.add((day.isoformat(),race));new_races+=1
        except Exception as exc:
            failed=True;issues.append((str(day),race,repr(exc)))
            audit.loc[len(audit)]=[day.isoformat(),str(race),'error','0',repr(exc)[:400]]
            print('失敗',day,race,repr(exc)[:120],flush=True)
    next_day=day if failed else day+timedelta(days=1)
    save_checkpoint(next_day)
    if failed:break
    day=next_day
print('今回追加レース',new_races,'確認日数',checked_days,'総行数',len(all_rows),'問題',len(issues),flush=True)
