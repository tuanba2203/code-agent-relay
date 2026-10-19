import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from accounts import Accounts,atomic
from test_accounts import auth
from smart_proxy import SmartProxy,ProxyError,SSEObserver,CodexTransport,proxy_handler


def message(text,role='user'):
    return {'type':'message','role':role,'content':[{'type':'input_text' if role=='user' else 'output_text','text':text}]}


def completed(identity='resp_1',output=None):
    return {'id':identity,'status':'completed','output':output if output is not None else [message('done','assistant')]}


def stream(result):
    return ('event: response.completed\r\ndata: '+json.dumps({'type':'response.completed','response':result})+'\r\n\r\ndata: [DONE]\n\n').encode()


class Response(io.BytesIO):
    def __init__(self,data,kind='text/event-stream'):
        super().__init__(data);self.headers={'Content-Type':kind}


class Transport:
    def __init__(self):self.calls=[];self.outputs=[]
    def open(self,body,credentials,headers,compact=False):
        self.calls.append((body,dict(credentials),dict(headers),compact))
        value=self.outputs.pop(0) if self.outputs else completed('resp_'+str(len(self.calls)))
        if isinstance(value,Exception):raise value
        if compact:return Response(json.dumps(value).encode(),'application/json')
        return Response(value if isinstance(value,bytes) else stream(value))


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();home=self.root/'home';home.mkdir()
        (home/'auth.json').write_bytes(auth('a'))
        self.manager=Accounts(self.root/'vault',home,guard=lambda:None)
        self.a=self.manager.capture('Alice')['id']
        file=self.root/'b.auth';file.write_bytes(auth('b',access='test-b-access'));self.b=self.manager.capture('Bob',file)['id']
        self.usage={key:{'primary':{'used_percent':20,'window_minutes':300},
                         'secondary':{'used_percent':10,'window_minutes':10080},'refresh':{'status':'ok'}} for key in (self.a,self.b)}
        self.collector=SimpleNamespace(_read_profile=lambda p:{'usage':copy.deepcopy(self.usage[p['id']])})
        self.options={'reserve':15,'margin':10,'disabled':[],'strategy':'reset_first'}
        self.transport=Transport();self.operation_lock=threading.Lock()
        self.service=self.make();self.auth_before=(home/'auth.json').read_bytes()

    def make(self):
        if hasattr(self,'service'):self.service.close()
        service=SmartProxy(self.manager,self.collector,lambda:self.options,self.operation_lock,self.transport)
        self.addCleanup(service.close)
        return service

    def request(self,items=None,ref=None,key='thread-one',compact=False,writer=None):
        body={'model':'test-model','input':items if items is not None else [message('task')],'stream':True,'store':False}
        if ref:body['previous_response_id']=ref
        raw=json.dumps(body,indent=2).encode();chunks=[];starts=[]
        self.service.forward(key,body,raw,{'session_id':key,'authorization':'Bearer local'},
                             lambda *args:starts.append(args),writer or chunks.append,compact)
        return body,b''.join(chunks),raw

    def test_same_account_forwards_identical_bytes_and_full_reasoning(self):
        self.transport.outputs=[completed('resp_A',[{'type':'reasoning','encrypted_content':'opaque'},message('answer','assistant')])]
        _,out,raw=self.request()
        self.assertEqual(self.transport.calls[0][0],raw)
        self.assertIn(b'opaque',out)
        self.request([message('next')],ref='resp_A')
        forwarded=json.loads(self.transport.calls[-1][0])
        self.assertEqual(forwarded['previous_response_id'],'resp_A')
        self.assertEqual([c[1]['access_token'] for c in self.transport.calls],['test-access','test-access'])
        self.assertEqual(self.service.sessions['thread-one']['account'],self.a)
        self.assertEqual((self.manager.home/'auth.json').read_bytes(),self.auth_before)

    def test_handoff_rebases_full_history_for_first_and_later_requests(self):
        self.request()
        anchor=copy.deepcopy(self.service.sessions['thread-one']['raw_history'])
        self.transport.outputs=[completed('summary',[message('Checkpoint: finish project','assistant')])]
        self.service.checkpoint('thread-one',self.b)
        first=anchor+[message('continue')]
        self.request(first)
        sent=json.loads(self.transport.calls[-1][0])
        self.assertEqual(len(sent['input']),2)
        self.assertIn('Checkpoint',sent['input'][0]['content'][0]['text'])
        self.assertEqual(sent['input'][1],message('continue'))
        self.assertEqual(self.transport.calls[-1][1]['access_token'],'test-b-access')
        self.assertNotIn('previous_response_id',sent)
        second=self.service.sessions['thread-one']['raw_history']+[message('next')]
        self.request(second)
        sent2=json.loads(self.transport.calls[-1][0])
        self.assertNotIn(message('task'),sent2['input'])
        self.assertEqual(sent2['input'][0],sent['input'][0])
        self.assertEqual(sent2['input'][-1],message('next'))
        self.assertEqual(self.service.sessions['thread-one']['generation'],1)
        self.assertEqual((self.manager.home/'auth.json').read_bytes(),self.auth_before)

    def test_handoff_replaces_old_reference_only_once(self):
        self.request()
        ref=self.service.sessions['thread-one']['last_response']
        self.service.checkpoint('thread-one',self.b)
        self.request([message('next')],ref=ref)
        self.assertNotIn('previous_response_id',json.loads(self.transport.calls[-1][0]))
        new_ref=self.service.sessions['thread-one']['last_response']
        self.request([message('again')],ref=new_ref)
        self.assertEqual(json.loads(self.transport.calls[-1][0])['previous_response_id'],new_ref)
        before=len(self.transport.calls)
        with self.assertRaises(ProxyError):self.request([message('old')],ref=ref)
        self.assertEqual(len(self.transport.calls),before)

    def test_mismatched_history_after_handoff_is_not_sent(self):
        self.request();self.service.checkpoint('thread-one',self.b)
        before=len(self.transport.calls)
        with self.assertRaises(ProxyError) as error:self.request([message('different history')])
        self.assertEqual(error.exception.code,'context_mismatch');self.assertEqual(len(self.transport.calls),before)

    def test_low_quota_blocks_new_user_request_without_switching(self):
        self.request();self.usage[self.a]['primary']['used_percent']=86;self.service.quota.clear()
        before=len(self.transport.calls)
        with self.assertRaises(ProxyError) as error:self.request([message('new task')])
        self.assertEqual(error.exception.code,'handoff_required');self.assertEqual(len(self.transport.calls),before)
        self.assertEqual(self.service.sessions['thread-one']['account'],self.a)

    def test_low_quota_tool_output_stays_on_source(self):
        self.request();self.usage[self.a]['primary']['used_percent']=90;self.service.quota.clear()
        self.request([{'type':'function_call_output','call_id':'c1','output':'ok'}],ref='resp_1')
        self.assertEqual(self.service.sessions['thread-one']['account'],self.a)

    def test_pending_tool_call_cannot_checkpoint(self):
        self.transport.outputs=[completed('resp_1',[{'type':'function_call','call_id':'c1','name':'shell','arguments':'{}'}])]
        self.request()
        with self.assertRaises(ProxyError) as error:self.service.checkpoint('thread-one',self.b)
        self.assertEqual(error.exception.code,'tool_loop_active')
        self.assertEqual(len(self.transport.calls),1)

    def test_commentary_does_not_hide_outstanding_tool_calls(self):
        self.transport.outputs=[completed('resp_1',[{'type':'function_call','call_id':'c1','name':'shell','arguments':'{}'},message('working','assistant')])]
        self.request()
        with self.assertRaises(ProxyError) as error:self.service.checkpoint('thread-one',self.b)
        self.assertEqual(error.exception.code,'tool_loop_active')

    def test_second_proxy_cannot_mutate_active_journal(self):
        self.request();s=self.service.sessions['thread-one'];s['inflight']=True;self.service._save(s)
        with self.assertRaises(ProxyError):
            SmartProxy(self.manager,self.collector,lambda:self.options,self.operation_lock,self.transport)
        self.assertTrue(self.service.sessions['thread-one']['inflight'])

    def test_reference_only_history_is_not_a_complete_checkpoint(self):
        self.request([{'type':'item_reference','id':'msg_old'}])
        with self.assertRaises(ProxyError) as error:self.service.checkpoint('thread-one',self.b)
        self.assertEqual(error.exception.code,'incomplete_history')

    def test_unknown_previous_context_cannot_checkpoint(self):
        self.request([message('delta')],ref='resp_unknown')
        with self.assertRaises(ProxyError) as error:self.service.checkpoint('thread-one',self.b)
        self.assertEqual(error.exception.code,'incomplete_history')

    def test_stream_disconnect_marks_uncertain_and_is_never_retried(self):
        self.transport.outputs=[b'data: {"type":"response.created"}\n\n']
        with self.assertRaises(ProxyError):self.request()
        self.assertEqual(self.service.sessions['thread-one']['state'],'uncertain')
        with self.assertRaises(ProxyError):self.request()
        self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(self.make().sessions['thread-one']['state'],'uncertain')

    def test_downstream_disconnect_marks_uncertain(self):
        def broken(_):raise BrokenPipeError()
        with self.assertRaises(ProxyError):self.request(writer=broken)
        self.assertEqual(self.service.sessions['thread-one']['state'],'uncertain')

    def test_disabled_target_never_gets_summary_or_route(self):
        self.request();self.options['disabled']=[self.b]
        with self.assertRaises(ProxyError):self.service.checkpoint('thread-one',self.b)
        self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(self.service.sessions['thread-one']['account'],self.a)

    def test_checkpoint_failure_keeps_source_and_no_handoff(self):
        self.request();self.transport.outputs=[b'data: {"type":"error"}\n\n']
        with self.assertRaises(ProxyError):self.service.checkpoint('thread-one',self.b)
        self.assertEqual(self.service.sessions['thread-one']['account'],self.a)
        self.assertFalse(list(self.service.directory.glob('handoff-*.md')))

    def test_restart_preserves_route_and_rebase(self):
        self.request();self.service.checkpoint('thread-one',self.b)
        self.service=self.make()
        self.request([message('next')],ref='resp_1')
        self.assertEqual(self.service.sessions['thread-one']['account'],self.b)
        self.assertIn('Relay checkpoint',json.loads(self.transport.calls[-1][0])['input'][0]['content'][0]['text'])

    def test_restart_inflight_is_uncertain_without_replay(self):
        self.request();s=self.service.sessions['thread-one'];s['inflight']=True;self.service._save(s)
        service=self.make()
        self.assertEqual(service.sessions['thread-one']['state'],'uncertain');self.assertEqual(len(self.transport.calls),1)

    def test_busy_thread_does_not_send_another_request(self):
        self.request();lock=self.service.locks['thread-one'];lock.acquire()
        try:
            with self.assertRaises(ProxyError):self.request()
            with self.assertRaises(ProxyError):self.service.checkpoint('thread-one',self.b)
        finally:lock.release()
        self.assertEqual(len(self.transport.calls),1)

    def test_near_reset_waits_without_summary_or_account_change(self):
        import time
        self.request();self.usage[self.a]['primary'].update(used_percent=90,resets_at=time.time()+120)
        self.service.quota.clear()
        with self.assertRaises(ProxyError) as error:self.request()
        self.assertEqual(error.exception.code,'waiting_reset')
        self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(self.service.sessions['thread-one']['account'],self.a)

    def test_target_loses_quota_after_summary_route_stays_on_source(self):
        self.request()
        original=self.transport.open
        def opened(*args,**kwargs):
            self.usage[self.b]['primary']['used_percent']=99
            return original(*args,**kwargs)
        with patch.object(self.transport,'open',opened):
            with self.assertRaises(ProxyError):self.service.checkpoint('thread-one',self.b)
        self.assertEqual(self.service.sessions['thread-one']['account'],self.a)
        self.assertFalse(list(self.service.directory.glob('handoff-*.md')))

    def test_failed_route_persistence_does_not_switch_in_memory(self):
        self.request();original=self.service._save
        def save(s):
            if s['account']==self.b:raise OSError('disk full')
            return original(s)
        with patch.object(self.service,'_save',save):
            with self.assertRaises(OSError):self.service.checkpoint('thread-one',self.b)
        self.assertEqual(self.service.sessions['thread-one']['account'],self.a)
        self.assertEqual(self.service.sessions['thread-one']['state'],'uncertain')

    def test_alias_is_persistent_without_model_calls(self):
        self.request();self.service.rename('thread-one','Website migration')
        self.assertEqual(self.make().sessions['thread-one']['alias'],'Website migration')
        self.assertEqual(len(self.transport.calls),1)

    def test_native_compaction_aligns_new_client_history(self):
        self.request();self.service.checkpoint('thread-one',self.b)
        anchor=copy.deepcopy(self.service.sessions['thread-one']['anchor'])
        compacted=[{'type':'compaction','encrypted_content':'new-account-state'}]
        self.transport.outputs=[{'output':compacted}]
        self.request(anchor,compact=True)
        self.assertNotIn('handoff',self.service.sessions['thread-one'])
        self.request(compacted+[message('next')])
        self.assertEqual(json.loads(self.transport.calls[-1][0])['input'],compacted+[message('next')])

    def test_logs_and_status_do_not_expose_credentials_or_context(self):
        self.request([message('private task')]);view=json.dumps(self.service.status())
        logs=(self.service.directory/'events.json').read_text()
        for value in ('test-access','test-refresh','private task',self.service.key):
            self.assertNotIn(value,view);self.assertNotIn(value,logs)
        self.assertIn('Alice',view)
        self.assertEqual((self.service.directory/'client-key').stat().st_mode&0o777,0o600)

    def test_http_auth_origin_and_successful_sse_forwarding(self):
        def http(headers):
            body=json.dumps({'model':'test','stream':True,'input':[message('task')]}).encode()
            fields={'Host':'127.0.0.1:8767','Content-Length':str(len(body)),**headers}
            wire=b'POST /v1/responses HTTP/1.0\r\n'+''.join(k+': '+v+'\r\n' for k,v in fields.items()).encode()+b'\r\n'+body
            class Connection:
                def __init__(self):self.data=bytearray()
                def makefile(self,*args,**kwargs):return io.BytesIO(wire)
                def sendall(self,data):self.data.extend(data)
            connection=Connection();proxy_handler(self.service)(connection,('127.0.0.1',1),SimpleNamespace())
            return bytes(connection.data)
        self.assertIn(b'401',http({'session_id':'s'}).split(b'\r\n')[0])
        headers={'Authorization':'Bearer '+self.service.key,'session_id':'s'}
        self.assertIn(b'403',http({**headers,'Origin':'https://evil.example'}).split(b'\r\n')[0])
        result=http(headers);self.assertIn(b'200',result.split(b'\r\n')[0]);self.assertIn(b'response.completed',result)


class ObserverTests(unittest.TestCase):
    def test_arbitrary_chunks_multibyte_and_crlf(self):
        data=stream(completed(output=[message('tiếng Việt','assistant')]))
        parser=SSEObserver()
        for byte in data:parser.feed(bytes([byte]))
        self.assertEqual(parser.finish()['output'][0]['content'][0]['text'],'tiếng Việt')

    def test_done_is_not_confirmed_completion(self):
        observer=SSEObserver();observer.feed(b'data: [DONE]\n\n')
        with self.assertRaises(ProxyError):observer.finish()

    def test_transport_replaces_authorization_and_rejects_redirects(self):
        transport=CodexTransport()
        with patch.object(transport.opener,'open',return_value=Response(b'')) as call:
            transport.open(b'{}',{'access_token':'secret','account_id':'workspace'},
                           {'authorization':'Bearer rogue','host':'evil','cookie':'secret','session_id':'thread'})
            request=call.call_args.args[0]
            self.assertEqual(request.full_url,'https://chatgpt.com/backend-api/codex/responses')
            self.assertEqual(request.get_header('Authorization'),'Bearer secret')
            self.assertIsNone(request.get_header('Cookie'))
