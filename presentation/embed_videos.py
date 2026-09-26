"""Embed existing MP4s into Artifact Tool slides using standard PowerPoint media XML.

Preserves native slide content and poster placement. Does not use python-pptx.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile, ZIP_DEFLATED
from lxml import etree as ET

P='http://schemas.openxmlformats.org/presentationml/2006/main'
A='http://schemas.openxmlformats.org/drawingml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL='http://schemas.openxmlformats.org/package/2006/relationships'
P14='http://schemas.microsoft.com/office/powerpoint/2010/main'
CT='http://schemas.openxmlformats.org/package/2006/content-types'
NS={'p':P,'a':A,'r':R,'p14':P14}

def element(parent,ns,tag,attrib=None):
 return ET.SubElement(parent,'{'+ns+'}'+tag,attrib or {})

def xmlbytes(root):
 return ET.tostring(root,xml_declaration=True,encoding='utf-8',standalone=True)

def embed(src:Path,dst:Path,root:Path):
 with ZipFile(src) as z: parts={i.filename:z.read(i.filename) for i in z.infolist()}
 entries=[]
 for number,stem in [(3,'luna-runway-closure'),(4,'luna-emergency'),(5,'luna-wind-shift')]:
  slidepath=f'ppt/slides/slide{number}.xml'
  tree=ET.fromstring(parts[slidepath])
  pics=tree.findall('.//p:pic',NS)
  if len(pics)!=1:raise ValueError(f'Expected exactly one movie poster on slide {number}')
  pic=pics[0];nv=pic.find('p:nvPicPr',NS);props=nv.find('p:cNvPr',NS)
  shapeid=props.get('id');props.set('name',stem+'.mp4');props.set('descr','Actual GPT-6 Luna simulation replay')
  element(props,A,'hlinkClick',{'action':'ppaction://media'})
  nvpr=nv.find('p:nvPr',NS)
  videoid='rIdATCVideo';mediaid='rIdATCMedia'
  element(nvpr,A,'videoFile',{'{'+R+'}link':videoid})
  extlist=element(nvpr,P,'extLst')
  ext=element(extlist,P,'ext',{'uri':'{DAA4B4D4-6D71-4841-9C94-3DE7FCFB9230}'})
  element(ext,P14,'media',{'{'+R+'}embed':mediaid})
  timing=tree.find('p:timing',NS)
  if timing is not None:raise ValueError('Refuse to overwrite existing animation timing')
  timing=element(tree,P,'timing');nodes=element(timing,P,'tnLst');par=element(nodes,P,'par')
  common=element(par,P,'cTn',{'id':'1','dur':'indefinite','restart':'never','nodeType':'tmRoot'})
  children=element(common,P,'childTnLst');video=element(children,P,'video')
  media=element(video,P,'cMediaNode',{'vol':'80000'})
  ct=element(media,P,'cTn',{'id':'2','fill':'hold','display':'0'})
  conds=element(ct,P,'stCondLst');element(conds,P,'cond',{'delay':'indefinite'})
  target=element(media,P,'tgtEl');element(target,P,'spTgt',{'spid':shapeid})
  parts[slidepath]=xmlbytes(tree)
  relpath=f'ppt/slides/_rels/slide{number}.xml.rels'
  rels=ET.fromstring(parts[relpath])
  for relid,kind in [(videoid,R+'/video'),(mediaid,'http://schemas.microsoft.com/office/2007/relationships/media')]:
   if any(r.get('Id')==relid for r in rels):raise ValueError('Media relationship id collision')
   element(rels,REL,'Relationship',{'Id':relid,'Type':kind,'Target':f'../media/{stem}.mp4'})
  parts[relpath]=xmlbytes(rels)
  data=(root/'atc_bench/web/videos'/f'{stem}.mp4').read_bytes()
  parts[f'ppt/media/{stem}.mp4']=data
  entries.append({'slide':number,'file':stem+'.mp4','sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'shape_id':shapeid})
 content=ET.fromstring(parts['[Content_Types].xml'])
 if not any(x.get('Extension')=='mp4' for x in content):element(content,CT,'Default',{'Extension':'mp4','ContentType':'video/mp4'})
 parts['[Content_Types].xml']=xmlbytes(content)
 with ZipFile(dst,'w',ZIP_DEFLATED) as z:
  for name,data in parts.items():z.writestr(name,data)
 # Validate the package itself, including exact source media and both relationships.
 with ZipFile(dst) as z:
  for entry in entries:
   assert hashlib.sha256(z.read('ppt/media/'+entry['file'])).hexdigest()==entry['sha256']
   slide=ET.fromstring(z.read(f"ppt/slides/slide{entry['slide']}.xml"))
   assert len(slide.findall('.//p14:media',NS))==1
   assert len(slide.findall('.//a:videoFile',NS))==1
   assert slide.find('.//p:spTgt',NS).get('spid')==entry['shape_id']
   assert slide.find('.//a:hlinkClick',NS).get('action')=='ppaction://media'
 (dst.parent/'media-verification.json').write_text(json.dumps(entries,indent=2)+'\n')
 print(f'Embedded and verified {len(entries)} MP4 files, {sum(e["bytes"] for e in entries):,} bytes total')

if __name__=='__main__':
 embed(*(Path(p).resolve() for p in sys.argv[1:4]))
