"""Versioned Owner acceptance policy; legacy JSON is imported on first save."""
import hashlib
import json
import sys
from pathlib import Path

from .core import DomainError, digest, encoded


class Acceptance:
    def __init__(self, host):
        self.host, self.core = host, host.core

    def read(self, conn):
        row=conn.execute("SELECT value FROM metadata WHERE key='owner_acceptance'").fetchone()
        if row:
            return json.loads(row['value'])
        path=self.host.root/'.local/acceptance.json'
        try:
            if path.stat().st_size>1024*1024:
                raise DomainError('OWNER_CHECK_CONFIGURATION_TOO_LARGE')
            raw=path.read_bytes()
        except FileNotFoundError:
            return dict(revision=0,source='file',config={'checks':{},'protected_paths':[]},file_hash=None)
        try:
            config=json.loads(raw.decode('utf-8-sig'))
        except (ValueError,UnicodeError):
            config=None
        return dict(revision=0,source='file',config=config,file_hash=hashlib.sha256(raw).hexdigest())

    @staticmethod
    def validate(config):
        if not isinstance(config,dict) or set(config)-{'checks','protected_paths'}:
            raise DomainError('INVALID_ACCEPTANCE_CONFIGURATION')
        checks,protected=config.get('checks'),config.get('protected_paths',[])
        if not isinstance(checks,dict) or not 1<=len(checks)<=100:
            raise DomainError('INVALID_ACCEPTANCE_CHECKS')
        for name,argv in checks.items():
            if (not isinstance(name,str) or not name.strip() or len(name)>100 or
                not isinstance(argv,list) or not 1<=len(argv)<=100 or
                any(not isinstance(a,str) or '\0' in a or len(a)>4096 for a in argv) or
                not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file() or
                Path(argv[0]).suffix.lower() in {'.cmd','.bat','.ps1'}):
                raise DomainError('INVALID_ACCEPTANCE_COMMAND')
        if (not isinstance(protected,list) or len(protected)>100 or any(
            not isinstance(p,str) or not p.strip() or len(p)>512 or '\\' in p or ':' in p or
            '\0' in p or p.startswith('/') or '..' in p.split('/') for p in protected)):
            raise DomainError('INVALID_PROTECTED_PATH')
        return {'checks':checks,'protected_paths':protected}

    def load(self):
        with self.core.connection() as conn:
            return self.read(conn)['config']

    @staticmethod
    def automatic_config():
        script=Path(__file__).resolve().parents[2]/'tools'/'pm_auto_check.py'
        return {'checks':{'pm_auto':[sys.executable,str(script)]},'protected_paths':[]}

    def bootstrap(self, *, key):
        """Install PM-owned verification and migrate unstarted legacy tasks."""
        def action(conn):
            current=self.read(conn)
            try:
                validated=self.validate(current['config'])
            except DomainError:
                validated=None
            if current.get('source') != 'pm_auto' or validated is None:
                # Existing hand-maintained commands are not a prerequisite for
                # delegation. Keep their original task intent below, while PM
                # owns and installs the executable verification policy.
                validated=self.automatic_config()
                self.validate(validated)
                record=dict(revision=current['revision']+1,source='pm_auto',config=validated)
                conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('owner_acceptance',encoded(record)))
                self.core._event(conn,'PMAcceptanceBootstrapped','pm',revision=record['revision'])
            migrated=[]
            for task in conn.execute("SELECT id,acceptance FROM tasks WHERE state='todo'").fetchall():
                prior=json.loads(task['acceptance'])
                if prior==['pm_auto']:
                    continue
                conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                             ('task_business_acceptance:'+task['id'],encoded(prior)))
                conn.execute('UPDATE tasks SET acceptance=?,acceptance_hash=?,revision=revision+1 WHERE id=?',
                             (encoded(['pm_auto']),digest(['pm_auto']),task['id']))
                self.core._event(conn,'TaskVerificationAutomated','pm',task=task['id'],prior_acceptance_hash=digest(prior))
                migrated.append(task['id'])
            return {'config':validated,'migrated_task_ids':migrated}
        return self.core._write('pm','bootstrap_acceptance',key,{'mode':'automatic'},action)

    def snapshot(self):
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            record=self.read(conn)
            tasks=[dict(r) for r in conn.execute("SELECT id,state,acceptance FROM tasks WHERE state!='cancelled'")]
        error=None
        try:
            self.validate(record['config'])
        except DomainError as exc:
            error=exc.code
        checks=(record['config'] or {}).get('checks',{}) if isinstance(record['config'],dict) else {}
        if not isinstance(checks,dict): checks={}
        return dict(**record,fingerprint=digest(record),validation_error=error,
            active_tasks=[t['id'] for t in tasks if t['state'] in {'leased','verifying'}],
            uncovered_tasks=[t['id'] for t in tasks if any(c not in checks for c in json.loads(t['acceptance']))])

    def save(self, fingerprint, config, *, key):
        def action(conn):
            validated=self.validate(config)
            current=self.read(conn)
            if digest(current)!=fingerprint:
                raise DomainError('ACCEPTANCE_VERSION_CONFLICT')
            record=dict(revision=current['revision']+1,source='database',config=validated)
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('owner_acceptance',encoded(record)))
            self.core._event(conn,'OwnerAcceptanceChanged','human',revision=record['revision'],
                             previous_hash=digest(current),configuration_hash=digest(validated))
            return dict(**record,fingerprint=digest(record))
        return self.core._write('human','save_acceptance',key,dict(fingerprint=fingerprint,config=config),action)
