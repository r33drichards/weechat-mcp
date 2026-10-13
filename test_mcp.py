# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
import weechat_mcp as m

class Fake:
    WEECHAT_RC_OK = 0
    def __init__(self): self.calls=[]
    def config_get_plugin(self, name): return 'irc.railway.#wchat'
    def infolist_get(self, kind, ptr, args):
        rows = ([{'full_name':'irc.railway.#wchat','pointer':'p'}, {'full_name':'irc.railway.#private','pointer':'secret'}] if kind=='buffer' else [{'command': c,'description':c} for c in ['msg','say','me','topic','names','who','exec','set','python']])
        return {'rows':rows,'i':-1}
    def infolist_next(self, il): il['i']+=1; return il['i'] < len(il['rows'])
    def infolist_string(self,il,key): return il['rows'][il['i']].get(key,'')
    infolist_pointer=infolist_string
    def infolist_free(self,il): pass
    def buffer_get_string(self,ptr,name): return '#wchat'
    def command(self,ptr,text): self.calls.append((ptr,text)); return 0

class Tests(unittest.TestCase):
    def setUp(self):
        self.w=Fake(); self.api=m.WeeChatAPI(self.w); self.p=m.Protocol(self.api)
    def req(self,method,params=None): return self.p.handle({'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
    def init(self): return self.req('initialize',{'protocolVersion':'2025-11-25'})
    def test_initialization(self):
        self.assertIn('error',self.req('tools/list')); self.assertEqual(self.init()['result']['protocolVersion'],'2025-11-25')
        self.assertIsNone(self.p.handle({'jsonrpc':'2.0','method':'notifications/initialized'}))
    def test_scope(self):
        self.assertEqual(list(self.api.buffers()),['irc.railway.#wchat'])
        with self.assertRaises(ValueError): self.api.call('weechat_msg',{'buffer':'irc.railway.#private','arguments':'hi'})
    def test_tools_are_discovered_but_shell_not_exposed(self):
        names=[t['name'] for t in self.api.tools()]
        self.assertIn('weechat_msg',names); self.assertNotIn('weechat_exec',names)
        self.assertTrue(any(c['command']=='exec' and not c['exposed'] for c in self.api.call('weechat_command_catalog',{})))
        with self.assertRaises(ValueError): self.api.call('weechat_exec',{'buffer':'irc.railway.#wchat','arguments':'id'})
    def test_send_target_is_fixed(self):
        result=self.api.call('weechat_msg',{'buffer':'irc.railway.#wchat','arguments':'hello'})
        self.assertTrue(result['accepted']); self.assertEqual(self.w.calls,[('p','/msg * hello')])
    def test_no_injection(self):
        for text in ['hello\n/exec id','hello\rJOIN #private','hi\0bye','x'*351]:
            with self.assertRaises(ValueError): self.api.call('weechat_say',{'buffer':'irc.railway.#wchat','arguments':text})
        self.assertFalse(self.w.calls)
    def test_topic_cannot_override_channel(self):
        self.api.call('weechat_topic',{'buffer':'irc.railway.#wchat','arguments':'#private hello'})
        self.assertEqual(self.w.calls[0][1],'/topic #wchat #private hello')
    def test_names_target_fixed(self):
        self.api.call('weechat_names',{'buffer':'irc.railway.#wchat'})
        self.assertEqual(self.w.calls[0][1],'/names #wchat')
        with self.assertRaises(ValueError): self.api.call('weechat_names',{'buffer':'irc.railway.#wchat','arguments':'#private'})
    def test_resource_subscription(self):
        self.init(); uri=self.api.uri('irc.railway.#wchat')
        self.req('resources/subscribe',{'uri':uri}); self.assertIn(uri,self.p.subscriptions)
        self.assertIn('contents',self.req('resources/read',{'uri':uri})['result'])
        self.req('resources/unsubscribe',{'uri':uri}); self.assertNotIn(uri,self.p.subscriptions)
        self.assertIn('error',self.req('resources/subscribe',{'uri':self.api.uri('irc.railway.#private')}))
    def test_ring_bound(self):
        self.api.lines['irc.railway.#wchat'].extend(range(250))
        self.assertEqual(len(self.api.history('irc.railway.#wchat')['lines']),200)
    def test_tool_errors_are_mcp_results(self):
        self.init(); r=self.req('tools/call',{'name':'bad','arguments':{}})
        self.assertTrue(r['result']['isError'])


class TransportTests(unittest.TestCase):
    def test_real_stdio_bridge_roundtrip_and_notification(self):
        import tempfile, socket, subprocess, os, json, select
        with tempfile.TemporaryDirectory() as d:
            path=os.path.join(d,'bridge.sock')
            listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
            listener.bind(('127.0.0.1',0)); listener.listen(1)
            proc=subprocess.Popen(['python3',os.path.join(os.path.dirname(__file__),'bridge.py'),'--loopback-port',str(listener.getsockname()[1])],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                listener.settimeout(3); peer,_=listener.accept(); peer.settimeout(3)
                request=b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
                proc.stdin.write(request); proc.stdin.flush()
                self.assertEqual(peer.recv(65536),request)
                notification=b'{"jsonrpc":"2.0","method":"notifications/resources/updated","params":{"uri":"weechat://buffer/test"}}\n'
                peer.sendall(notification)
                self.assertTrue(select.select([proc.stdout],[],[],3)[0])
                self.assertEqual(proc.stdout.readline(),notification)
                proc.stdin.close(); self.assertEqual(proc.wait(timeout=3),0)
                peer.close()
            finally:
                if proc.poll() is None: proc.kill(); proc.wait()
                proc.stdout.close(); proc.stderr.close(); listener.close()

class RobustnessTests(unittest.TestCase):
    setUp = Tests.setUp
    req = Tests.req
    init = Tests.init
    def test_bad_params(self):
        self.assertIn('error',self.req('initialize',[1]))
    def test_bad_name(self):
        self.init(); self.assertTrue(self.req('tools/call',{'name':42})['result']['isError'])
    def test_control_characters(self):
        with self.assertRaises(ValueError): self.api.call('weechat_msg',{'buffer':'irc.railway.#wchat','arguments':'\x01VERSION\x01'})

if __name__=='__main__': unittest.main()
