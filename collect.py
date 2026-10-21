"""Monbetsu race result collector for GitHub Actions. Existing results.csv is preserved."""
import json
import os
import random
import re
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

START_DATE = '2023-01-01'
END_DATE = os.environ.get('END_DATE', date.today().isoformat())
MAX_RUN_MINUTES = int(os.environ.get('MAX_RUN_MINUTES', '48'))
WAIT_SECONDS = float(os.environ.get('WAIT_SECONDS', '0.6'))
ROOT = Path(__file__).resolve().parent
DATA, LOG, CURSOR = (ROOT / x for x in ('results.csv', 'v6_audit.csv', 'v6_cursor.json'))
STATUS = ROOT / 'collection_status.json'
BASE = 'https://www.hokkaidokeiba.net/raceinfo/'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; ResearchCollector/1.0)'})


def get_soup(url):
    last = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=25)
            response.raise_for_status()
            time.sleep(WAIT_SECONDS + random.uniform(0, 0.25))
            return BeautifulSoup(response.content, 'html.parser')
        except requests.RequestException as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'éä¿¡å¤±æ {url}: {last}')


def txt(cell):
    return cell.get_text(' ', strip=True).replace('\u3000', ' ')


def expand_table(table):
    pending, output = {}, []
    for tr in table.select('tr'):
        row, col = [], 0
        def fill():
            nonlocal col
            while col in pending:
                value, left = pending[col]
                row.append(value)
                if left <= 1:
                    del pending[col]
                else:
                    pending[col] = (value, left - 1)
                col += 1
        fill()
        for cell in tr.find_all(['td', 'th'], recursive=False):
            fill()
            value = txt(cell)
            for _ in range(int(cell.get('colspan', 1))):
                row.append(value)
                if int(cell.get('rowspan', 1)) > 1:
                    pending[col] = (value, int(cell.get('rowspan', 1)) - 1)
                col += 1
            fill()
        fill()
        if row:
            output.append(row)
    return output


def race_links(day):
    stamp = day.strftime('%Y%m%d')
    soup = get_soup(BASE + f'index.php?p_day={stamp}')
    numbers = set()
    for a in soup.find_all('a', href=True):
        url = urljoin(BASE, a['href'])
        query = parse_qs(urlparse(url).query)
        if 'seiseki.php' in url and query.get('p_day', [''])[0] == stamp:
            try:
                numbers.add(int(query['p_rno'][0]))
            except (KeyError, ValueError):
                pass
    return sorted(numbers)


def race_tables(day, num):
    query = f'p_day={day.strftime("%Y%m%d")}&p_rno={num:03d}'
    result_page = get_soup(BASE + 'seiseki.php?' + query)
    odds_page = get_soup(BASE + 'odds/tanhuku.php?' + query)
    def choose(soup, words):
        for table in soup.select('table'):
            header = ' '.join(txt(cell) for cell in table.select('th'))
            if all(word in header for word in words):
                return expand_table(table)
        raise ValueError(f'è¡¨ãè¦ã¤ãããªã: {words}')
    return choose(result_page, ['çé ', 'é¦¬çª', 'é¦¬å']), choose(odds_page, ['é¦¬çª', 'ååãªããº'])


def table_records(rows):
    header = [re.sub(r'\s+', '', s) for s in rows[0]]
    if 'è² æ' in header and 'éé' in header:
        index = header.index('è² æ')
        header[index] = 'æ¤é'
        header.pop(header.index('éé'))
    records = []
    for index, row in enumerate(rows[1:], 1):
        if len(row) != len(header):
            raise ValueError(f'è¡åä¸ä¸è´ row={index} header={header} row={row}')
        records.append(dict(zip(header, row)))
    return records


def parse_race(day, num):
    result_rows, odds_rows = race_tables(day, num)
    results, odds = table_records(result_rows), table_records(odds_rows)
    def horse_no(value):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None
    def numeric(value):
        try:
            return float(str(value).replace(',', ''))
        except (TypeError, ValueError):
            return None
    odds_by_no = {horse_no(record.get('é¦¬çª')): record for record in odds}
    if not results:
        raise ValueError('çµæ0è¡')
    if len(odds_by_no) < len(results) - 2:
        raise ValueError(f'ãªããºè¡ä¸è¶³ çµæ{len(results)} ãªããº{len(odds_by_no)}')
    output = []
    for record in results:
        number = horse_no(record.get('é¦¬çª'))
        if number is None:
            continue
        odd = odds_by_no.get(number, {})
        output.append({
            'éå¬æ¥': day.isoformat(), 'ã¬ã¼ã¹': num, 'é¦¬çª': number,
            'é¦¬å': record.get('é¦¬å'), 'çé ': horse_no(record.get('çé ')),
            'é¨æ': record.get('é¨æ'), 'èª¿æå¸«': record.get('èª¿æå¸«'),
            'æ çª': record.get('æ çª'), 'æ§é½¢': record.get('æ§é½¢'),
            'æ¤é': record.get('æ¤é', record.get('è² æéé')),
            'é¦¬ä½é': record.get('é¦¬ä½é'), 'èµ°ç ´ã¿ã¤ã ': record.get('ã¿ã¤ã '),
            'çå·®': record.get('çå·®'), 'ååãªããº': numeric(odd.get('ååãªããº')),
            'è¤åãªããºç¯å²': odd.get('è¤åãªããº')
        })
    if not output:
        raise ValueError('é¦¬çªã®è§£æå¤±æ')
    return output


def atomic_csv(frame, path):
    temporary = path.with_name(path.name + '.tmp')
    frame.to_csv(temporary, index=False, encoding='utf-8-sig')
    os.replace(temporary, path)


def main():
    old = pd.read_csv(DATA, encoding='utf-8-sig', low_memory=False) if DATA.exists() else pd.DataFrame(columns=['éå¬æ¥', 'ã¬ã¼ã¹', 'é¦¬çª'])
    all_rows = old.to_dict('records')
    audit = pd.read_csv(LOG, dtype=str).fillna('') if LOG.exists() else pd.DataFrame(columns=['date', 'race', 'status', 'count', 'message'])
    completed = set()
    for row in all_rows:
        try:
            completed.add((str(row['éå¬æ¥'])[:10], int(row['ã¬ã¼ã¹'])))
        except (KeyError, ValueError, TypeError):
            pass
    for record in audit.itertuples(index=False):
        try:
            key = (str(record.date), int(record.race))
            if record.status == 'ok':
                completed.add(key)
            elif record.status == 'error':
                completed.discard(key)
        except (ValueError, AttributeError):
            pass
    start, end = date.fromisoformat(START_DATE), date.fromisoformat(END_DATE)
    try:
        cursor = date.fromisoformat(json.loads(CURSOR.read_text())['next_date'])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        cursor = start
    cursor = max(cursor, start)
    # Once historical collection is finished, refresh the latest three days.
    if cursor > end:
        cursor = max(start, end - timedelta(days=2))
        completed = {key for key in completed if key[0] < cursor.isoformat()}
    initial_cursor = cursor
    clock = time.monotonic()
    cutoff = MAX_RUN_MINUTES * 60
    added, checked, issues = 0, 0, []

    def checkpoint(next_day):
        nonlocal all_rows
        frame = pd.DataFrame(all_rows)
        if not frame.empty:
            frame = frame.drop_duplicates(['éå¬æ¥', 'ã¬ã¼ã¹', 'é¦¬çª'], keep='last')
            atomic_csv(frame, DATA)
            all_rows = frame.to_dict('records')
        atomic_csv(audit, LOG)
        temporary = CURSOR.with_suffix('.json.tmp')
        temporary.write_text(json.dumps({'next_date': next_day.isoformat()}, ensure_ascii=False))
        os.replace(temporary, CURSOR)

    print('æ¢å­è¡æ°:', len(all_rows), 'éå§æ¥:', cursor, 'çµäºæ¥:', end, flush=True)
    day = cursor
    while day <= end:
        if time.monotonic() - clock >= cutoff:
            break
        checked += 1
        try:
            nums = race_links(day)
        except Exception as exc:
            issues.append((str(day), 'ä¸è¦§', repr(exc)))
            checkpoint(day)
            break
        remaining = [number for number in nums if (day.isoformat(), number) not in completed]
        failed = False
        if remaining:
            print('éå¬', day, 'æªåå¾', len(remaining), flush=True)
        for number in remaining:
            if time.monotonic() - clock >= cutoff:
                failed = True
                break
            try:
                rows = parse_race(day, number)
                all_rows = [row for row in all_rows if not (str(row.get('éå¬æ¥')) == day.isoformat() and str(row.get('ã¬ã¼ã¹')) == str(number))]
                all_rows.extend(rows)
                audit.loc[len(audit)] = [day.isoformat(), str(number), 'ok', str(len(rows)), '']
                completed.add((day.isoformat(), number))
                added += 1
            except Exception as exc:
                failed = True
                issues.append((str(day), number, repr(exc)))
                audit.loc[len(audit)] = [day.isoformat(), str(number), 'error', '0', repr(exc)[:400]]
                print('å¤±æ', day, number, repr(exc)[:120], flush=True)
        next_day = day if failed else day + timedelta(days=1)
        checkpoint(next_day)
        if failed:
            break
        day = next_day
    # Status is committed together with CSV. Follow-up run is only dispatched if progress was made.
    try:
        final_cursor = date.fromisoformat(json.loads(CURSOR.read_text())['next_date'])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        final_cursor = day
    historical_pending = final_cursor <= end
    progress = final_cursor > initial_cursor or added > 0
    status = {
        'updated_at_utc': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        'next_date': final_cursor.isoformat(), 'end_date': end.isoformat(),
        'historical_pending': historical_pending, 'made_progress': progress,
        'continue_automatically': historical_pending and progress and not issues,
        'added_races': added, 'checked_days': checked,
        'total_rows': len(all_rows), 'errors': len(issues),
    }
    temporary = STATUS.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, STATUS)
    print('ä»åè¿½å ã¬ã¼ã¹', added, 'ç¢ºèªæ¥æ°', checked, 'ç·è¡æ°', len(all_rows), 'åé¡', len(issues), flush=True)
    print('èªåç¶ç¶:', status['continue_automatically'], 'æ¬¡åéå§æ¥:', final_cursor, flush=True)


if __name__ == '__main__':
    main()
