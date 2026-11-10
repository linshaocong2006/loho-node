"""Seven-day SQLite history and authenticated, paged static JSON exports."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from identities import Resolver

RETENTION = 7 * 86400
PAGE_SIZE = 500
LIVE_LIMIT = 1000
MAX_LINE = 65536
READ_BUDGET = 4 * 1024 * 1024


def atomic_json(path, payload):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(str(temporary), 0o640)
        json.dump(payload, stream, ensure_ascii=False, separators=(',', ':'))
        stream.flush()
    os.replace(str(temporary), str(path))


class History:
    def __init__(self, directory):
        self.identities = Resolver()
        self.identity_signature = None
        self.directory = Path(directory)
        self.public = self.directory / 'history'
        self.public.mkdir(exist_ok=True)
        os.chmod(str(self.public), 0o750)
        self.db = sqlite3.connect(str(self.directory / 'history.sqlite3'))
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA cache_size=-4096')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('PRAGMA journal_size_limit=8388608')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS connections (
                minute INTEGER NOT NULL, hour INTEGER NOT NULL,
                user TEXT NOT NULL, source TEXT NOT NULL, host TEXT NOT NULL,
                port TEXT NOT NULL, protocol TEXT NOT NULL,
                domain INTEGER NOT NULL, dns INTEGER NOT NULL,
                first REAL NOT NULL, last REAL NOT NULL, count INTEGER NOT NULL,
                PRIMARY KEY(minute,user,source,host,port,protocol)
            );
            CREATE INDEX IF NOT EXISTS connections_last ON connections(last DESC);
            CREATE INDEX IF NOT EXISTS connections_user_last ON connections(user,last DESC);
            CREATE INDEX IF NOT EXISTS connections_hour ON connections(hour,last DESC);
            CREATE TABLE IF NOT EXISTS cursors (
                identity TEXT PRIMARY KEY, position INTEGER NOT NULL,
                anchor TEXT NOT NULL, discarded INTEGER NOT NULL,
                touched REAL NOT NULL
            );
        ''')
        self.db.commit()
        self.dirty = set(row[0] for row in self.db.execute('SELECT DISTINCT hour FROM connections'))
        self.last_by_user = {}
        self.last_by_source = {}
        self.manifest = {}
        self.version = 0
        self.last_prune = 0
        self.last_archive = 0
        self.archive_pending = True
        self.importing = True

    def close(self):
        self.db.close()

    def add(self, event, now=None):
        now = time.time() if now is None else now
        if event is None or event['ts'] < now - RETENTION:
            return
        self.identities.refresh()
        event = dict(event, user=self.identities.resolve(event['user'], event['ts']))
        minute = int(event['ts'] // 60)
        hour = int(event['ts'] // 3600)
        key = (minute,event['user'],event['source'],event['host'],event['port'],event['protocol'])
        self.db.execute('''INSERT OR IGNORE INTO connections
            (minute,hour,user,source,host,port,protocol,domain,dns,first,last,count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,0)''',
            (minute,hour,event['user'],event['source'],event['host'],event['port'],event['protocol'],
             int(event['domain']),int(event['dns']),event['ts'],event['ts']))
        self.db.execute('''UPDATE connections SET first=MIN(first,?),last=MAX(last,?),count=count+1
            WHERE minute=? AND user=? AND source=? AND host=? AND port=? AND protocol=?''',
            (event['ts'],event['ts']) + key)
        self.dirty.add(hour)
        timestamp = event['ts']
        user, source = event['user'], event['source']
        self.last_by_user[user] = max(timestamp, self.last_by_user.get(user, timestamp))
        self.last_by_source[source] = max(timestamp, self.last_by_source.get(source, timestamp))
        self.version += 1

    def as_row(self, row):
        result = self.identities.row(row)
        result.pop('minute',None)
        result.pop('hour',None)
        result['domain'] = bool(result['domain'])
        result['dns'] = bool(result['dns'])
        return result

    def prune(self, now):
        if now-self.last_prune < 60:
            return
        cutoff = now-RETENTION
        self.dirty.update(row[0] for row in self.db.execute('SELECT DISTINCT hour FROM connections WHERE last < ?', (cutoff,)))
        deleted = self.db.execute('DELETE FROM connections WHERE last < ?', (cutoff,)).rowcount
        self.db.execute('DELETE FROM cursors WHERE touched < ?', (cutoff,))
        self.db.commit()
        if not self.last_prune:
            self.last_by_user = dict(self.db.execute('SELECT user, MAX(last) FROM connections GROUP BY user'))
            self.last_by_source = dict(self.db.execute('SELECT source, MAX(last) FROM connections GROUP BY source'))
        elif deleted:
            self.last_by_user = {user: last for user, last in self.last_by_user.items() if last >= cutoff}
            self.last_by_source = {source: last for source, last in self.last_by_source.items() if last >= cutoff}
        if deleted:
            self.archive_pending = True
            self.version += 1
        self.last_prune = now

    def export(self, output, error=None, now=None, force=False):
        now = time.time() if now is None else now
        self.identities.refresh()
        if self.identity_signature != self.identities.signature:
            self.dirty.update(row[0] for row in self.db.execute('SELECT DISTINCT hour FROM connections'))
            self.identity_signature = self.identities.signature
            self.archive_pending = True
        self.prune(now)
        self.db.commit()
        archive_due = force or self.archive_pending or not self.last_archive or now-self.last_archive>=60
        if archive_due:
            processed = sorted(self.dirty,reverse=True)[:4]
            for hour in processed:
                cursor = self.db.execute('SELECT * FROM connections WHERE hour=? ORDER BY last DESC,user,source,host,port,protocol', (hour,))
                generation = secrets.token_hex(5)
                pages,users,sources = [],set(),set()
                groups,records,index = 0,0,0
                while True:
                    chunk = cursor.fetchmany(PAGE_SIZE)
                    if not chunk:
                        break
                    rows = [self.as_row(row) for row in chunk]
                    for row in rows:
                        users.add(row['identity'])
                        sources.add(row['source'])
                        records += row['count']
                    filename = 'h-%s-%s-%s.json' % (hour,generation,index)
                    atomic_json(self.public/filename,{'schema':2,'rows':rows})
                    pages.append({'file':filename,'rows':len(rows)})
                    groups += len(rows)
                    index += 1
                if groups:
                    self.manifest[hour] = {'hour':hour,'start':hour*3600,'end':(hour+1)*3600,
                        'groups':groups,'records':records,'users':sorted(users),'sources':sorted(sources),'pages':pages}
                else:
                    self.manifest.pop(hour,None)
            self.dirty.difference_update(processed)
            self.archive_pending = bool(self.dirty)
        hours = sorted(self.manifest.values(),key=lambda item:item['hour'],reverse=True)
        cutoff = now - RETENTION
        seen = {user: last for user, last in self.last_by_user.items() if last >= cutoff}
        shared = {'schema':2,'generated':now,'version':self.version,
            'retention_seconds':RETENTION,'error':error,'importing':self.importing,
            'users':sorted(seen),'account_labels':{k:a['label'] for k,a in self.identities.value['accounts'].items()},'identity_cutover':self.identities.value.get('cutover'),
            'sources':sorted(source for source, last in self.last_by_source.items() if last >= cutoff),
            'archive_pending':self.archive_pending,
            'total_groups':self.db.execute('SELECT COUNT(*) FROM connections').fetchone()[0]}
        shared['account_last_seen'] = {user[4:]:last for user,last in seen.items() if user.startswith('key:')}
        shared['user_last_seen'] = {user:last for user,last in seen.items() if not user.startswith('key:')}
        atomic_json(Path(output).with_name('activity.json'),dict(schema=1,generated=now,version=self.version,error=error,importing=self.importing,account_last_seen=shared['account_last_seen'],user_last_seen=shared['user_last_seen'],identity_cutover=shared['identity_cutover']))
        if archive_due:
            atomic_json(self.public/'index.json',dict(shared,hours=hours))
            self.last_archive = now
            current = {page['file'] for hour in hours for page in hour['pages']}
            for file in self.public.glob('h-*.json'):
                if self.archive_pending:break
                expired_hour = int(file.name.split('-')[1])+1 <= (now-RETENTION)//3600
                if file.name not in current and (expired_hour or file.stat().st_mtime<now-180):
                    file.unlink()
        live = [self.as_row(row) for row in self.db.execute('SELECT * FROM connections ORDER BY last DESC LIMIT ?', (LIVE_LIMIT,))]
        atomic_json(Path(output),dict(shared,rows=live,max_groups=LIVE_LIMIT,truncated=shared['total_groups']>LIVE_LIMIT))


def identity(info):
    return '%s:%s' % (info.st_dev,info.st_ino)


def anchor(stream,position):
    saved = stream.tell()
    stream.seek(max(0,position-128))
    digest = hashlib.sha256(stream.read(min(position,128))).hexdigest()
    stream.seek(saved)
    return digest


class DurableFollower:
    def __init__(self,log,history,parser):
        self.path,self.history,self.parser = Path(log),history,parser
        self.stream = None
        self.key = None
        self.discarded = False

    def close(self):
        if self.stream:
            self.stream.close()
        self.stream = None

    def _open(self):
        self.stream = self.path.open('rb')
        info = os.fstat(self.stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            self.close()
            raise OSError('日志不是普通文件')
        self.key = identity(info)
        stored = self.history.db.execute('SELECT * FROM cursors WHERE identity=?',(self.key,)).fetchone()
        if stored and stored['position']<=info.st_size and anchor(self.stream,stored['position'])==stored['anchor']:
            self.stream.seek(stored['position'])
            self.discarded = bool(stored['discarded'])
        else:
            self.stream.seek(0)
            self.discarded = False

    def _save_cursor(self):
        position = self.stream.tell()
        self.history.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,?,?,?)',
            (self.key,position,anchor(self.stream,position),int(self.discarded),time.time()))

    def _consume(self,budget=READ_BUDGET):
        consumed = 0
        while consumed<budget:
            start = self.stream.tell()
            raw = self.stream.readline(MAX_LINE+1)
            if not raw:
                break
            consumed += len(raw)
            complete = raw.endswith(b'\n')
            if self.discarded or len(raw)>MAX_LINE:
                self.discarded = not complete
                continue
            if not complete:
                self.stream.seek(start)
                break
            self.history.add(self.parser(raw.decode('utf-8',errors='replace')))
        self.history.importing = consumed>=budget
        self._save_cursor()
        return consumed<budget

    def read_available(self):
        try:
            if self.stream is None:
                self._open()
            info = self.path.stat()
            if identity(info)!=self.key:
                if not self._consume():
                    self.history.db.commit()
                    return
                self.close()
                self._open()
            else:
                stored = self.history.db.execute('SELECT * FROM cursors WHERE identity=?',(self.key,)).fetchone()
                if info.st_size<self.stream.tell() or (stored and anchor(self.stream,stored['position'])!=stored['anchor']):
                    self.stream.seek(0)
                    self.discarded = False
            self._consume()
            self.history.db.commit()
        except BaseException:
            self.history.db.rollback()
            self.history.last_prune = 0
            self.close()
            raise
