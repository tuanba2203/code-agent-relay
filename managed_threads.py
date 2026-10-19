"""Private registry of logical work streams and per-account Codex conversations."""
import fcntl
import json
import os
from contextlib import contextmanager
import time
import uuid
from accounts import AccountError, atomic, read_private, clean_alias


class ManagedThreads:
    def __init__(self,directory):
        self.directory=directory
        self.path=directory/'managed-threads.json'
        self.lock_path=directory/'managed-threads.lock'

    @contextmanager
    def transaction(self):
        fd=os.open(self.lock_path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            if self.path.exists():
                try:data=json.loads(read_private(self.path))
                except ValueError:raise AccountError('Managed-thread registry is unreadable; automatic recreation is blocked') from None
                if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('workflows'),dict):
                    raise AccountError('Invalid managed-thread registry; restore its backup before continuing')
                for key,flow in data['workflows'].items():
                    if (not isinstance(flow,dict) or flow.get('id')!=key
                            or not isinstance(flow.get('label'),str) or not isinstance(flow.get('cwd'),str)
                            or not isinstance(flow.get('bindings'),dict) or not isinstance(flow.get('history'),list)):
                        raise AccountError('Invalid managed workflow record; restore its backup')
                    for account,binding in flow['bindings'].items():
                        if (not isinstance(account,str) or not isinstance(binding,dict)
                                or not isinstance(binding.get('thread_id'),str)
                                or binding.get('state') not in ('ready','prepared')):
                            raise AccountError('Invalid account/thread binding; restore its backup')
                    if any(not isinstance(e,dict) or not isinstance(e.get('job_id'),str) for e in flow['history']):
                        raise AccountError('Invalid managed workflow history')
            else:data={'version':1,'workflows':{}}
            before=json.dumps(data,sort_keys=True)
            yield data['workflows']
            if json.dumps(data,sort_keys=True)!=before:
                if self.path.exists():atomic(self.directory/'managed-threads.previous.json',read_private(self.path))
                atomic(self.path,data)
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

    def listing(self):
        with self.transaction() as workflows:
            return json.loads(json.dumps(list(workflows.values())))

    def get(self,key):
        with self.transaction() as workflows:
            if key not in workflows:raise AccountError('Unknown managed workflow')
            return json.loads(json.dumps(workflows[key]))

    def ensure(self,account,thread,label,cwd):
        label=clean_alias(' '.join(str(label).split())[:80]) or 'Luồng công việc'
        with self.transaction() as workflows:
            for flow in workflows.values():
                binding=flow['bindings'].get(account)
                if binding and binding['thread_id']==thread:
                    return flow['id']
                if any(b['thread_id']==thread for b in flow['bindings'].values()):
                    raise AccountError('This conversation is tracked on another account; select its managed workflow instead')
            key=uuid.uuid4().hex
            workflows[key]={'id':key,'label':label,'cwd':cwd,'created_at':time.time(),
                            'bindings':{account:{'thread_id':thread,'state':'ready','updated_at':time.time(),
                                                 'name_status':'pending'}},
                            'history':[]}
            return key

    def rename(self,key,label):
        label=clean_alias(label)
        if not label:raise AccountError('Workflow label must not be empty')
        with self.transaction() as workflows:
            if key not in workflows:raise AccountError('Unknown managed workflow')
            workflows[key]['label']=label
            for binding in workflows[key]['bindings'].values():
                binding['name_status']='pending'
        return self.get(key)

    def bind(self,key,account,thread,state='ready'):
        with self.transaction() as workflows:
            if key not in workflows:raise AccountError('Unknown managed workflow')
            for other in workflows.values():
                for acct,binding in other['bindings'].items():
                    if binding['thread_id']==thread and (other['id']!=key or acct!=account):
                        raise AccountError('Conversation is already bound to another workflow or account')
            previous=workflows[key]['bindings'].get(account)
            if previous and previous['thread_id']!=thread:
                raise AccountError('Account already has a thread for this workflow; replacement is not automatic')
            binding=dict(previous or {})
            binding.update(thread_id=thread,state=state,updated_at=time.time())
            if binding.get('synced_name')!=workflows[key]['label']:binding['name_status']='pending'
            workflows[key]['bindings'][account]=binding
        return self.get(key)

    def name_result(self,key,account,label,error=None,name=None):
        with self.transaction() as workflows:
            flow=workflows[key]
            binding=flow['bindings'][account]
            if flow['label']!=label:return
            binding['name_status']='error' if error else 'synced'
            if error:binding['name_error']=error
            else:
                binding['synced_name']=name if name is not None else label
                binding.pop('name_error',None)

    def transition(self,key,event):
        """Upsert bounded recent history; the job journals retain full history."""
        with self.transaction() as workflows:
            if key not in workflows:raise AccountError('Unknown managed workflow')
            history=workflows[key]['history']
            existing=next((e for e in history if e['job_id']==event['job_id']),None)
            if existing:existing.update(event,updated_at=time.time())
            else:history.append(dict(event,updated_at=time.time()))
            workflows[key]['history']=history[-100:]
