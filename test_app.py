import sys,os,json,time,urllib.request,urllib.error,subprocess,re,tempfile
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)  # firmware: MP100NSP.BIN next to this script
import test_iso as T
tmp=tempfile.mkdtemp()
vob=T.make_vob(os.path.join(tmp,'t.vob')); vmg,vts=T.fake_ifos()
tree={'README.TXT':b'hello karaoke\n'*50,'VIDEO_TS':{'VIDEO_TS.IFO':vmg,'VTS_01_0.IFO':vts,'VTS_01_1.VOB':open(vob,'rb').read()}}
iso=os.path.join(tmp,'test_dvd.iso'); open(iso,'wb').write(T.build_iso9660(tree))
PORT=8799
srv=subprocess.Popen([sys.executable,'karaoke_emu.py','gui','--no-browser','--port',str(PORT),'--fw',os.path.join(HERE,'MP100NSP.BIN'),'--iso',iso],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,cwd=HERE)
time.sleep(2.0)
base=f'http://127.0.0.1:{PORT}'
ok=[];bad=[]
def chk(n,c,x=''): (ok if c else bad).append(n); print(('PASS ' if c else 'FAIL ')+n+('' if c else '  '+str(x)))
def get(path,tok=None,host=None,raw=False):
    h={}
    if tok: h['X-Token']=tok
    if host: h['Host']=host
    try:
        r=urllib.request.urlopen(urllib.request.Request(base+path,headers=h)); b=r.read(); return r.status,(b if raw else json.loads(b)) if b else b
    except urllib.error.HTTPError as e:
        b=e.read()
        try: return e.code,json.loads(b)
        except Exception: return e.code,b
def post(path,body,tok):
    try:
        r=urllib.request.urlopen(urllib.request.Request(base+path,data=json.dumps(body).encode(),headers={'X-Token':tok,'Content-Type':'application/json'})); return r.status,json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code,json.loads(e.read())
try:
    s,page=get('/',raw=True); page=page.decode()
    chk('page served',s==200 and 'Project : Chiaki' in page and 'Firmware debug messages' in page and 'Drive module' in page)
    tok=re.search(r'const TOKEN="([0-9a-f]+)"',page).group(1); chk('token embedded',len(tok)==32)
    chk('no token -> 403',get('/api/status')[0]==403)
    chk('wrong token -> 403',get('/api/status','deadbeef'*4)[0]==403)
    chk('foreign Host header -> 403 (DNS-rebinding guard)',get('/api/status',tok,'evil.example.com')[0]==403)
    s,c=get('/api/config',tok); chk('config reports fw + iso',c['fw'].endswith('MP100NSP.BIN') and c['iso'].endswith('test_dvd.iso'),c)
    s,st=get('/api/status',tok); chk('status idle before boot',st['state']=='idle',st)
    s,ls=get('/api/disc/ls?path=/',tok); chk('disc ls root',[e['name'] for e in ls['entries']]==['VIDEO_TS','README.TXT'],ls)
    s,ls=get('/api/disc/ls?path=/VIDEO_TS',tok); chk('disc ls subdir',len(ls['entries'])==3,ls)
    s,ls=get('/api/disc/ls?path=/NOPE',tok); chk('disc ls missing -> 400 w/ message',s==400 and 'error' in ls,(s,ls))
    s,png=get('/api/disc/thumb',tok,raw=True); chk('disc thumbnail is a PNG',s==200 and png[:4]==b'\x89PNG',(s,png[:80]))
    out=os.path.join(tmp,'out_test.vob')
    s,r=post('/api/disc/extract',{'path':'/VIDEO_TS/VTS_01_1.VOB','dest':out},tok)
    chk('extract writes file',s==200 and os.path.getsize(out)==r['bytes']==os.path.getsize(vob),r)
    chk('extract is byte-exact',open(out,'rb').read()==open(vob,'rb').read())
    s,r=post('/api/disc/extract',{'path':'/README.TXT','dest':''},tok); chk('extract without dest -> 400',s==400,r)
    s,r=post('/api/disc/open',{'path':os.path.join(tmp,'nonexistent.iso')},tok); chk('open missing disc -> 400',s==400,r)
    junk=os.path.join(tmp,'junk.iso'); open(junk,'wb').write(os.urandom(50000))
    s,r=post('/api/disc/open',{'path':junk},tok); chk('open garbage disc -> 400 w/ message',s==400 and 'error' in r,r)
    s,r=post('/api/disc/open',{'path':iso},tok); chk('reopen valid disc',s==200 and r['type']=='DVD-Video',r)
    s,r=post('/api/boot',{'fw':os.path.join(tmp,'does_not_exist.bin')},tok); chk('boot with bad firmware path -> 400',s==400,r)
    s,r=post('/api/boot',{'budget':8_000_000},tok); chk('boot starts',s==200,r)
    time.sleep(1); s,r2=post('/api/boot',{'budget':8_000_000},tok)
    for _ in range(120):
        s,st=get('/api/status',tok)
        if st['state'] in ('done','error','stopped'): break
        time.sleep(1)
    chk('second boot while running rejected (or finished)',(r2.get('error') or st['state']!='running'),(r2,))
    chk('short boot finished',st['state']=='done',st)
    chk('UART banner captured',('ROM START' in st['uart']),st['uart'][:120])
    chk('status carries drive + debug fields',('drive' in st and 'fw_debug' in st),list(st))
finally:
    srv.terminate()
print(f'\n{len(ok)} passed, {len(bad)} failed')
sys.exit(1 if bad else 0)
