#!/usr/bin/env python3
"""Reuse upstream runtime payload checks. Static verification is NOT device testing."""
import argparse,csv,hashlib,importlib.util,json,os,re,subprocess,tomllib
from pathlib import Path
from zipfile import ZipFile
ABIS=('arm64-v8a','armeabi-v7a')
MODULES=('mpv','python','quickjs','nodejs','thunder','bdj')
def sha(p):
    with p.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()
def command(*args):
    p=subprocess.run(list(args),capture_output=True,text=True)
    if p.returncode: raise ValueError(p.stdout+'\n'+p.stderr)
    return p.stdout+p.stderr
p=argparse.ArgumentParser()
p.add_argument('directory',type=Path)
p.add_argument('--repo',type=Path,required=True)
p.add_argument('--version-code',type=int,required=True)
p.add_argument('--version-name',required=True)
a=p.parse_args(); a.directory=a.directory.resolve(); a.repo=a.repo.resolve()
logs=a.directory/'logs'; logs.mkdir(exist_ok=True)
manifest={'schema':1,'kind':'runtime-suite-static-check','status':'failed','gatePassed':False,'components':[],'errors':[],'source':command('git','-C',str(a.repo),'rev-parse','HEAD').strip(),'scope':'Static checks only. Not a release catalog or device test.'}
try:
    tools=Path(os.environ['ANDROID_HOME'])/'build-tools'/tomllib.loads((a.repo/'gradle/libs.versions.toml').read_text())['versions']['buildTools']
    spec=importlib.util.spec_from_file_location('catalog',a.repo/'scripts/runtime_catalog.py'); catalog=importlib.util.module_from_spec(spec);spec.loader.exec_module(catalog)
    statuses={(r['component'],r['abi']):r for r in csv.DictReader((logs/'build-status.tsv').open(),delimiter='\t')}
    for component in ('host',*MODULES):
        for abi in ABIS:
            entry={'component':component,'abi':abi,'status':'skipped'}; manifest['components'].append(entry)
            row=statuses.get((component,abi))
            if not row: entry['reason']='Not built';continue
            entry['build']=row
            if row['status']!='built' or row['exitCode']!='0': entry.update(status='failed',reason='Build/collection failure');continue
            name=('leanback-' if component=='host' else 'runtime-'+component+'-')+abi.replace('-','_')+'.apk'
            f=a.directory/('host' if component=='host' else 'runtime')/name
            try:
                cert=command(str(tools/'apksigner'),'verify','--verbose','--print-certs',str(f)); (logs/(name+'.signature.txt')).write_text(cert)
                certs=set(re.findall(r'^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]+)',cert,re.M))
                if len(certs)!=1: raise ValueError('Expected one signer')
                badging=command(str(tools/'aapt2'),'dump','badging',str(f));(logs/(name+'.badging.txt')).write_text(badging)
                fields=dict(re.findall(r"(\w+)='([^']*)'",badging.splitlines()[0]))
                expected='com.fongmi.android.tv' if component=='host' else 'com.fongmi.runtime.'+component
                if fields.get('name')!=expected or fields.get('versionCode')!=str(a.version_code) or fields.get('versionName')!=a.version_name: raise ValueError('Package/version mismatch')
                payload={'path':f.relative_to(a.directory).as_posix(),'sha256':sha(f),'size':f.stat().st_size,'signerSha256':next(iter(certs)),'packageName':expected}
                with ZipFile(f) as z:
                    if len(z.namelist())!=len(set(z.namelist())) or z.testzip(): raise ValueError('Invalid APK ZIP')
                    if any(n.endswith(('.jks','.keystore')) or n.endswith('/local.properties') for n in z.namelist()): raise ValueError('Private build input leaked')
                    if component=='host':
                        if 'classes.dex' not in z.namelist(): raise ValueError('Missing DEX')
                        for n in z.namelist():
                            if n.startswith('lib/') and n.endswith('.so'):
                                if n.split('/')[1]!=abi: raise ValueError('Wrong host ABI')
                                catalog.verify_elf(f,z.open(n),abi)
                        payload['hostCxxSha256']=hashlib.sha256(z.read(f'lib/{abi}/libc++_shared.so')).hexdigest()
                    else:
                        catalog.read_apk(f,component,abi,str(tools/'aapt2'))
                        metadata=catalog.read_runtime_metadata(f,z,component,abi)
                        payload['runtimeMetadata']=metadata
                entry.update(status='verified',artifact=payload)
            except Exception as e: entry.update(status='failed',reason=str(e))
    hosts={e['abi']:e for e in manifest['components'] if e['component']=='host' and e['status']=='verified'}
    for e in manifest['components']:
        if e['status']!='verified' or e['component']=='host':continue
        h=hosts.get(e['abi']); ar=e['artifact']
        if not h or ar['signerSha256']!=h['artifact']['signerSha256']:e.update(status='failed',reason='Signer differs from host')
        if e['component']=='mpv' and h and ar['runtimeMetadata']['hostLibraries']['libc++_shared.so']!=h['artifact']['hostCxxSha256']:e.update(status='failed',reason='MPV host C++ hash mismatch')
    core=[e for e in manifest['components'] if e['component'] in ('host','mpv','python')]
    manifest['gatePassed']=len(core)==6 and all(e['status']=='verified' for e in core)
    if manifest['gatePassed']:manifest['status']='pass' if all(e['status']=='verified' for e in manifest['components']) else 'partial'
except Exception as e:manifest['errors'].append(str(e))
(a.directory/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps(manifest,indent=2))
raise SystemExit(0 if manifest['gatePassed'] else 1)
