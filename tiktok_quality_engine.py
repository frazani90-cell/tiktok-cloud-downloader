#!/usr/bin/env python3
"""TikTok quality comparisons for a GitHub Actions + rclone archive.

Uses public yt-dlp/gallery-dl extraction only; no app-only access is claimed.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

VIDEO_EXT={'.mp4','.mov','.mkv','.webm'}
VIDEO_ID=re.compile(r'(?:^|_)(\d{17,21})(?:_|\.|$)')

def run(cmd, timeout=180, check=False):
    try:
        p=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if p.returncode and check: raise RuntimeError(p.stderr[-1200:])
        return p
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print('COMMAND FAILED:',cmd[0],str(e),flush=True)
        return None

def probe(path):
    p=run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],35)
    if not p or p.returncode: return None
    try:
        obj=json.loads(p.stdout); streams=[s for s in obj.get('streams',[]) if s.get('codec_type')=='video']
        if not streams:return None
        v=streams[0]; fmt=obj.get('format',{}); fps=v.get('avg_frame_rate','0/1')
        a,b=map(float,fps.split('/')) if '/' in fps else (float(fps),1.0)
        return dict(width=int(v.get('width') or 0),height=int(v.get('height') or 0),
                    fps=round(a/b,3) if b else 0,codec=str(v.get('codec_name','')),
                    bitrate=int(v.get('bit_rate') or fmt.get('bit_rate') or 0),
                    size=path.stat().st_size)
    except Exception as e:
        print('PROBE FAILED:',e);return None

def improves(old,new):
    if not new:return False
    if not old:return True
    # Pixel count measures dimensions regardless of portrait/landscape orientation.
    a=old['width']*old['height'];b=new['width']*new['height']
    if b>a*1.10:return True
    if b<a*0.98:return False
    # Same resolution: do not trade appreciably lower fps for bitrate.
    if new['fps']>old['fps']*1.20 and new['bitrate']>=old['bitrate']*0.8:return True
    if new['fps']<old['fps']*0.95:return False
    # Bitrate only comparable between same codecs; require material improvement.
    if new['codec']==old['codec'] and old['bitrate']>0 and new['bitrate']>old['bitrate']*1.30:
        return True
    return False

def candidates(url,tmp,do_ytdlp):
    out=[]
    if do_ytdlp:
        # Browser impersonation and the unmodified extractor can expose different formats.
        for i,imp in enumerate((True,False)):
            dest=tmp/f'yt{i}.%(ext)s'
            args=['yt-dlp','--no-playlist','--no-overwrites','--socket-timeout','20',
                  '--retries','2','--format','bv*+ba/b','--format-sort-force',
                  '--format-sort','res,fps,vbr,tbr','--merge-output-format','mp4',
                  '-o',str(dest)]
            if imp:args+=['--impersonate','chrome']
            p=run(args+[url],110)
            if not p or p.returncode:print('yt-dlp route',i,'not available:',(p.stderr if p else '')[-350:])
            out.extend(x for x in tmp.glob(f'yt{i}.*') if x.suffix.lower() in VIDEO_EXT)
    # Independent public extractor path; no re-encoding.
    gal=tmp/'gallery';gal.mkdir(exist_ok=True)
    p=run(['gallery-dl','-D',str(gal),'-f','source_{id}.{extension}',
           '-o','tiktok.photos=false','-o','tiktok.audio=false',
           '-o','tiktok.covers=false','-o','tiktok.videos=true',url],110)
    if not p or p.returncode:print('gallery-dl route not available:',(p.stderr if p else '')[-350:])
    out.extend(x for x in gal.rglob('*') if x.is_file() and x.suffix.lower() in VIDEO_EXT)
    return out

def compare_file(username,local,remote=None):
    vid=VIDEO_ID.search(local.name)
    if not vid:
        print('Skipping unrecognized filename:',local.name);return False
    url=f'https://www.tiktok.com/@{username}/video/{vid.group(1)}'
    baseline=probe(local)
    if not baseline:
        print('WARNING cannot inspect baseline, leaving untouched:',local.name);return False
    print('BASELINE',local.name,baseline,flush=True)
    best=local;bestinfo=baseline
    with tempfile.TemporaryDirectory(prefix='quality_') as td:
        for c in candidates(url,Path(td),bool(remote)):
            info=probe(c)
            if info:print('CANDIDATE',c.name,info,flush=True)
            if info and improves(bestinfo,info):best=c;bestinfo=info
        if best==local:
            print('KEEP existing:',local.name);return False
        # An extension change is not safe to silently conceal. Prefer MP4 replacement.
        if best.suffix.lower()!=local.suffix.lower():
            print('SKIP higher candidate with incompatible extension:',best.name)
            return False
        shutil.copy2(best,local)
        print('UPGRADED',local.name,'FROM',baseline,'TO',bestinfo,flush=True)
        return True

def remote_json(path,default):
    with tempfile.TemporaryDirectory() as t:
        target=Path(t)/'state.json';p=run(['rclone','copyto',path,str(target)],70)
        if not p or p.returncode:return default
        try:return json.loads(target.read_text())
        except Exception:return default

def upload_json(obj,remote):
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'state.json';p.write_text(json.dumps(obj,indent=2,sort_keys=True))
        r=run(['rclone','copyto',str(p),remote],100)
        if not r or r.returncode:raise RuntimeError('Could not save repair state')

def repair(username,limit):
    folder=f'gdrive:TikTok/{username}'
    r=run(['rclone','lsjson',folder,'--files-only','--max-depth','1'],110)
    if not r or r.returncode:
        raise RuntimeError('Could not list Drive folder for '+username+': '+(r.stderr if r else 'timeout'))
    files=[x['Name'] for x in json.loads(r.stdout)
           if Path(x.get('Name','')).suffix.lower() in VIDEO_EXT and VIDEO_ID.search(x.get('Name',''))]
    files.sort()
    state_path=folder+'/quality_recheck_state.json'
    state=remote_json(state_path,{'checked':{}})
    checked=state.setdefault('checked',{})
    # oldest checked first, never repeatedly stuck on undownloadable files
    files.sort(key=lambda f:checked.get(f,''))
    print(f'{username}: {len(files)} video files; checking {min(limit,len(files))}')
    failures=0;upgrades=0
    for name in files[:limit]:
        with tempfile.TemporaryDirectory(prefix='drive_video_') as td:
            local=Path(td)/name
            p=run(['rclone','copyto',folder+'/'+name,str(local)],120)
            if not p or p.returncode:
                failures+=1;print('WARNING baseline unavailable:',name)
                continue
            try:
                if compare_file(username,local,remote=folder+'/'+name):
                    r=run(['rclone','copyto',str(local),folder+'/'+name],120)
                    if not r or r.returncode:
                        failures+=1;print('WARNING upgrade upload failed:',name)
                    else:upgrades+=1
            except Exception as e:
                failures+=1;print('WARNING repair failed:',name,e)
            checked[name]=dt.datetime.now(dt.timezone.utc).isoformat()
            upload_json(state,state_path)
    print('RESULT:',username,'checked',min(limit,len(files)),'upgraded',upgrades,'warnings',failures)

def new(username,folder):
    targets=[p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT]
    # yt-dlp already downloaded these files; only test independent gallery-dl source.
    upgrades=0
    for p in targets:
        if compare_file(username,p):upgrades+=1
    print('NEW VIDEO QUALITY RESULT:',len(targets),'checked',upgrades,'upgraded')

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('mode',choices=['new','repair']);ap.add_argument('--username',required=True)
    ap.add_argument('--folder');ap.add_argument('--limit',type=int,default=5)
    a=ap.parse_args()
    if a.mode=='new':
        if not a.folder:ap.error('--folder is required for new mode')
        new(a.username,a.folder)
    else:repair(a.username,a.limit)
