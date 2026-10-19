"""Opt-in macOS desktop lifecycle. Never force-kills or handles VS Code."""
import subprocess
import sys
import time
from accounts import AccountError


class DesktopLifecycle:
    APPS=('/Applications/ChatGPT.app','/Applications/Codex.app')

    def processes(self):
        if sys.platform!='darwin':raise AccountError('Automatic desktop restart requires macOS')
        try:
            result=subprocess.run(['/bin/ps','-axo','pid=,comm='],capture_output=True,text=True,timeout=5,check=True)
        except (OSError,subprocess.SubprocessError):
            raise AccountError('Cannot inspect desktop processes') from None
        rows=[]
        for line in result.stdout.splitlines():
            parts=line.strip().split(None,1)
            if len(parts)!=2 or not parts[0].isdigit():continue
            for app in self.APPS:
                if parts[1].startswith(app+'/Contents/'):
                    rows.append({'app':app,'pid':int(parts[0])})
                    break
        return rows

    def plan(self):
        rows=self.processes()
        return [{'app':app,'pids':[r['pid'] for r in rows if r['app']==app]}
                for app in self.APPS if any(r['app']==app for r in rows)]

    def close(self,plan,cancel,timeout=30):
        for row in plan:
            app=row['app']
            if app not in self.APPS:raise AccountError('Unsupported desktop application')
            if cancel.is_set():raise AccountError('Desktop restart cancelled')
            # Constant allowlisted paths; no shell, process arguments or credentials.
            script='tell application "'+app+'" to quit'
            try:subprocess.run(['/usr/bin/osascript','-e',script],capture_output=True,timeout=10,check=True)
            except (OSError,subprocess.SubprocessError):
                raise AccountError('Desktop did not accept quit; close it manually. No force kill was performed.') from None
        deadline=time.monotonic()+timeout
        apps={r['app'] for r in plan}
        while True:
            if cancel.is_set():raise AccountError('Desktop restart cancelled')
            if not any(r['app'] in apps for r in self.processes()):return
            if time.monotonic()>=deadline:
                raise AccountError('Desktop processes still running after quit; close them manually. No force kill was performed.')
            cancel.wait(.5)

    def reopen(self,plan):
        for row in plan:
            if row['app'] not in self.APPS:raise AccountError('Unsupported desktop application')
            try:subprocess.run(['/usr/bin/open','-a',row['app']],capture_output=True,timeout=10,check=True)
            except (OSError,subprocess.SubprocessError):
                raise AccountError('Cannot reopen desktop; open the application manually') from None
