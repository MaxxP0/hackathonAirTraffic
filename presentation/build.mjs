import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {execFileSync} from 'node:child_process';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {Presentation,PresentationFile} from '@oai/artifact-tool';
process.env.RUNTIME_NODE_MODULES ||= path.resolve(path.dirname(process.execPath),'../node_modules');
const HERE=path.dirname(fileURLToPath(import.meta.url));
const ROOT=path.dirname(HERE);
const BUILD=path.join(HERE,'.build');
const SKILL=process.env.ATC_PRESENTATION_SKILL||path.join(os.homedir(),'.codex/plugins/cache/openai-primary-runtime/presentations/26.923.10815/skills/presentations');
const PYTHON=process.env.ATC_PRESENTATION_PYTHON||path.join(os.homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3');
const {resolvePresentationFont,applyPresentationChartFont,finalizePresentation}=await import(pathToFileURL(path.join(SKILL,'container_tools/artifact_tool_utils.mjs')));
const family=resolvePresentationFont({fontFamily:'Arial'});
const COLORS={bg:'#08121B',white:'#E5F0F5',muted:'#91A8B7',cyan:'#5DD4CA',orange:'#F9B75F',red:'#FF7892',grid:'#28404F'};
const presentation=Presentation.create({slideSize:{width:1280,height:720}});
await fs.mkdir(BUILD,{recursive:true});
const previewOnly=process.argv.includes('--draft');
const scenarios=['runway_closure','emergency','wind_shift'];
const labels={runway_closure:'Runway closure',emergency:'Emergencies',wind_shift:'Wind reversal'};
const readJSON=async p=>JSON.parse(await fs.readFile(path.join(ROOT,p),'utf8'));
const sourcePaths=[];
const results={};
for(const [model,folder] of [['Luna','results/luna-v3'],['GLM','results/glm-completed-v3']]){
 results[model]={};
 for(const scenario of scenarios){
  const file=`${folder}/openrouter-${scenario}-7.json`;
  let r;
  try{r=await readJSON(file);}catch(e){
   const published=`docs/evaluations/${model==='Luna'?'gpt-6-luna':'glm-5.3-flash'}-${scenario}-seed7.json`;
   try{r=await readJSON(published);}catch(e2){if(!previewOnly)throw e2;r={status:'pending'};}
  }
  if(!previewOnly&&(r.status!=='completed'||r.final_observation?.time_s!==1800||!r.final_observation?.done))throw new Error(`Incomplete comparison input: ${file}`);
  sourcePaths.push(file);results[model][scenario]=r;
 }
}
const landing=r=>{
 if(!r.final_observation)return {mean:null,counts:'pending'};
 const groups={landed:[],pending:[],failed:[]};
 const map={landing_roll:'landed',taxi_in:'landed',landed:'landed',inbound:'pending',holding:'pending',approach:'pending',diverting:'failed',diverted:'failed',crashed:'failed'};
 for(const a of r.final_observation.aircraft.filter(a=>a.kind==='arrival'))groups[map[a.status]].push(a.airborne_time_s);
 const all=Object.values(groups).flat();
 return {mean:all.reduce((a,b)=>a+b,0)/all.length,counts:Object.values(groups).map(g=>g.length).join(' / ')};
};
const minutes=x=>x==null?'n/a':(x/60).toFixed(1);
const metric=(r,k)=>r.status==='completed'?r.metrics[k]:null;
function text(s,t,x,y,w,h,size=28,color=COLORS.white,bold=false){
 const q=s.shapes.add({geometry:'textbox',position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
 q.text=t;q.text.style={typeface:family,fontSize:size,color,bold,autoFit:'none'};
 return q;
}
function base(title,note){const s=presentation.slides.add();s.background.fill=COLORS.bg;text(s,title,64,44,1152,70,44,COLORS.white,true);if(note)text(s,note,64,636,1152,55,19,COLORS.muted);return s;}
function notes(s,body,files=[]){s.speakerNotes.textFrame.setText(body+'\n\nSources: '+files.map(f=>'https://github.com/MaxxP0/hackathonAirTraffic/blob/main/'+f.replace(/^results\/luna-v3\/openrouter-(.+)-7\.json$/, 'docs/evaluations/gpt-6-luna-$1-seed7.json').replace(/^results\/glm-completed-v3\/openrouter-(.+)-7\.json$/, 'docs/evaluations/glm-5.3-flash-$1-seed7.json')).join('\n'));}
function table(s,values,{top=190,height=360,widths,font=23}={}){
 const t=s.tables.add({rows:values.length,columns:values[0].length,left:64,top,width:1152,height,values,columnWidths:widths});
 t.borders.assign({fill:COLORS.grid,width:0.6,style:'solid'});
 t.cells.block({row:0,column:0,rowCount:values.length,columnCount:values[0].length}).assign({fill:COLORS.bg,textStyle:{typeface:family,fontSize:font,color:COLORS.white},margins:{left:12,right:10,top:8,bottom:8},anchor:'center'});
 for(let r=0;r<values.length;r++){
  t.rows[r].height=height/values.length;
  for(let c=0;c<values[0].length;c++){
   const cell=t.getCell(r,c);cell.text.style={typeface:family,fontSize:font,color:r===0?COLORS.cyan:COLORS.white,bold:r===0,autoFit:'none'};
   if(r===0)cell.fill='#132532';
  }
 }
 return t;
}
{
 const s=presentation.slides.add();s.background.fill=COLORS.bg;
 text(s,'AIR TRAFFIC CONTROL',64,132,1152,50,24,COLORS.cyan,true);
 text(s,'An LLM at the controls',64,210,1152,100,68,COLORS.white,true);
 text(s,'GPT-6 Luna and GLM 5.3 Flash',64,337,1152,58,36);
 text(s,'Frankfurt-inspired simulation\nRecorded scenarios and benchmark results',64,458,1040,110,29,COLORS.muted);
 text(s,'26 September 2026',64,638,900,40,20,COLORS.muted);
 notes(s,'Synthetic benchmark results from command-driven simulation. This airport is a lightweight Frankfurt-inspired layout, not an operational airport digital twin.',sourcePaths);
}
{
 const s=base('Benchmark setup','Same traffic seed and 30-minute horizon for both models. One run per scenario.');
 text(s,'30 minutes',64,162,340,66,48,COLORS.cyan,true);
 text(s,'of simulated traffic',64,230,350,40,25);
 text(s,'120 seconds',660,162,490,66,48,COLORS.orange,true);
 text(s,'between controller decisions',660,230,530,40,25);
 text(s,'The model receives text telemetry and issues commands for approaches, runway assignments, headings and takeoffs.',64,330,1120,100,29);
 text(s,'The simulator pauses during inference. Each model keeps recent dialogue and a persistent operational plan.',64,468,1120,100,29);
 notes(s,'Seed 7. Scenario duration 1,800 simulated seconds. Decision interval 120 seconds, 15 successful decisions per completed episode. Both use the same frankfurt-controller-v3 prompt, max output 8,192 tokens and low reasoning. The provider does not support a temperature parameter for Luna. GLM uses temperature 0. GLM completion resumes saved state after provider errors. Pausing inference means API delays do not add simulated waiting time.',sourcePaths);
}
const videos=[
 {id:'luna-runway-closure',title:'Luna: sudden runway closure',body:'A closure forces CFG101 to go around.\nLuna assigns runway 25C.',foot:'32-second replay at 30× speed. The full episode recorded 3 diversions and 3 separation-loss events.'},
 {id:'luna-emergency',title:'Luna: emergency arrivals',body:'Luna resolves all three emergencies.\nMean emergency wait: 8.7 minutes.',foot:'36-second replay at 30× speed. The full episode recorded 7 separation-loss events.'},
 {id:'luna-wind-shift',title:'Luna: wind reversal',body:'The active direction changes from 25 to 07.\nDLH106 goes around and later lands on 07C.',foot:'35-second replay at 30× speed. The full episode recorded 9 separation-loss events.'}
];
for(const v of videos){
 const s=base(v.title,v.foot);
 // Poster becomes the embedded movie shape during OOXML packaging.
 s.images.add({blob:new Uint8Array(await fs.readFile(path.join(ROOT,'atc_bench/web/videos',v.id+'.jpg'))),contentType:'image/jpeg',alt:'ATC_VIDEO:'+v.id,fit:'contain',position:{left:64,top:154,width:840,height:472.5}});
 text(s,v.body,944,183,274,255,25);
 text(s,'Click the video in Slide Show to play',944,511,264,93,22,COLORS.cyan);
 notes(s,'This is an actual recorded GPT-6 Luna run. The MP4 is embedded in the PowerPoint. Replays speed up simulated time by 30 times. The full recorded trajectory was checked against the saved simulator result.',[`atc_bench/web/videos/${v.id}.run.json`,`atc_bench/web/videos/${v.id}.mp4`]);
}
{
 const s=base('Scores across the three scenarios','Higher is better. These scores combine throughput, safety penalties and waiting-time penalties.');
 const series=[['Luna',COLORS.cyan],['GLM',COLORS.orange]].filter(([m])=>scenarios.every(sc=>results[m][sc].status==='completed')).map(([m,fill])=>({name:m==='Luna'?'GPT-6 Luna':'GLM 5.3 Flash',values:scenarios.map(sc=>results[m][sc].metrics.score),fill,valuesFormatCode:'0.0'}));
 const chart=s.charts.add('bar',{position:{left:64,top:153,width:1152,height:451},categories:scenarios.map(sc=>labels[sc]),series,hasLegend:true,legend:{position:'bottom',textStyle:{typeface:family,fontSize:24,fill:COLORS.white}},barOptions:{direction:'column',grouping:'clustered',gapWidth:115},chartFill:COLORS.bg,chartLine:{fill:'none',width:0},plotAreaFill:COLORS.bg,plotAreaLine:{fill:'none',width:0},xAxis:{tickLabelPosition:'low',textStyle:{typeface:family,fontSize:23,fill:COLORS.white},line:{fill:COLORS.grid,width:1}},yAxis:{numberFormatCode:'0',textStyle:{typeface:family,fontSize:21,fill:COLORS.muted},majorGridlines:{fill:COLORS.grid,width:1}},dataLabels:{showValue:true,position:'outEnd',textStyle:{typeface:family,fontSize:24,bold:true,fill:COLORS.white}}});
 applyPresentationChartFont(chart,{fontFamily:family});
 if(series.length!==2)text(s,'GLM completion pending',810,110,410,44,22,COLORS.orange);
 notes(s,'Scores use final metrics.score only when status is completed and simulated time reaches 1,800 seconds. Bars are native editable PowerPoint charts. Negative values are valid penalties, not missing data. The benchmark also supplies a separate safety-first rank tuple. One seed does not establish general model superiority.',sourcePaths);
}
{
 const s=base('Waiting time and arrival outcomes','Means in minutes. Arrival outcomes show landed / pending / failed. Lower waits need to be read with outcomes.');
 const rows=[['Scenario','Model','Ground\nwait','Landing\ntime','Emergency\nwait','Arrival\noutcomes']];
 for(const sc of scenarios)for(const model of ['Luna','GLM']){
  const r=results[model][sc],l=landing(r),done=r.status==='completed';
  rows.push([labels[sc],model,done?minutes(r.metrics.ground_wait_mean_seconds):'pending',done?minutes(l.mean):'pending',done?minutes(r.metrics.emergency_wait_mean_seconds):'pending',done?l.counts:'pending']);
 }
 table(s,rows,{top:166,height:378,widths:[235,105,170,170,190,282],font:22});
 text(s,'Landing time starts at sector entry and includes normal approach flight. Pending arrivals contribute time observed at the 30-minute horizon.',64,565,1145,63,22,COLORS.muted);
 notes(s,'Ground wait is accumulated departure queue time until takeoff clearance. Emergency wait begins at declaration and ends at touchdown, crash or sector exit. Missing the emergency deadline records a failure but does not stop the timer. Landing service time is existing arrival airborne_time_s, stopping at touchdown, crash or sector exit. All spawned arrivals enter the means, including pending and failed outcomes. Landed here means touchdown, including aircraft still rolling or taxiing. Diverting, diverted and crashed arrivals count as failed. Landing time is a supplemental diagnostic and does not change the overall score. n/a means no emergency population.',sourcePaths);
}
{
 const s=base('Safety outcomes','Separation exposure sums seconds for each aircraft pair below both separation thresholds.');
 const rows=[['Scenario','Model','Collisions','Separation\nevents','Pair-minutes\nbelow minima','Wake\nviolations']];
 for(const sc of scenarios)for(const model of ['Luna','GLM']){
  const r=results[model][sc],done=r.status==='completed',m=r.metrics;
  rows.push([labels[sc],model,...(done?[m.collisions,m.separation_losses,minutes(m.separation_loss_seconds),m.wake_violations]:['pending','pending','pending','pending'])]);
 }
 table(s,rows,{top:166,height:400,widths:[235,105,150,210,272,180],font:22});
 notes(s,'A separation event counts the start of a pairwise loss of separation. Pair-minutes are summed exposure divided by 60. Simultaneous pairs each contribute. No collision does not imply safe separation. Threshold: horizontal distance below 3 NM and vertical separation below 1,000 ft. Collision threshold: horizontal below 0.06 NM and vertical below 100 ft.',sourcePaths.concat(['atc_bench/environment.py','docs/SCORING.md']));
}
{
 const s=base('Score interpretation and limits','Source code, recorded commands and full results: github.com/MaxxP0/hackathonAirTraffic');
 text(s,'Proximity penalties',64,157,545,50,32,COLORS.cyan,true);
 text(s,'Below 3 NM horizontally and 1,000 ft vertically:\n−0.5 points per pair per simulated second.',64,231,549,132,27);
 text(s,'Collision threshold: 0.06 NM and 100 ft.\n−1,000,000 points per collision, plus crash penalties.',64,400,549,139,27);
 text(s,'What this comparison establishes',678,157,538,83,32,COLORS.orange,true);
 text(s,'One seed per scenario provides an initial comparison. More seeds are needed to estimate consistency.',678,268,538,120,27);
 text(s,'GLM resumed from saved state after API interruptions. Provider and quantization changes may affect latency and model behavior.',678,438,538,145,27);
 notes(s,'The comparison uses synthetic traffic in a simplified Frankfurt-inspired airport. Detailed ground activity and BlueSky integration are outside this prototype. The score includes penalties for safety events, unresolved emergencies, delays and invalid commands, along with completion rewards. There is no gradual proximity penalty outside the fixed thresholds. GLM episodes preserve commands, simulator state, recent dialogue and long-term plan across resumption. Original successful GLM calls used InferenceNet. Resumed completion calls used Sail Research/fp8. Run records document provider routes. All external calls obeyed the user’s $10 provider limit.',sourcePaths.concat(['docs/SCORING.md','examples/resume_openrouter.py']));
}
const candidate=path.join(BUILD,'candidate.pptx');
await (await PresentationFile.exportPptx(presentation)).save(candidate);
for(let i=0;i<presentation.slides.items.length;i++){
 const blob=await presentation.export({slide:presentation.slides.items[i],format:'png',scale:1});
 await fs.writeFile(path.join(BUILD,`slide-${i+1}.png`),new Uint8Array(await blob.arrayBuffer()));
}
await fs.writeFile(path.join(BUILD,'inputs.json'),JSON.stringify({sources:sourcePaths,results:Object.fromEntries(Object.entries(results).map(([model,rs])=>[model,Object.fromEntries(Object.entries(rs).map(([scenario,r])=>[scenario,{status:r.status,metrics:r.metrics,landing:landing(r)}]))]))},null,2));
if(!previewOnly){
 const mediaCandidate=path.join(BUILD,'candidate-with-media.pptx');
 execFileSync(PYTHON,[path.join(HERE,'embed_videos.py'),candidate,mediaCandidate,ROOT],{stdio:'inherit'});
 const final=path.join(HERE,'output','ATC-Benchmark-Luna-vs-GLM.pptx');
 await fs.mkdir(path.dirname(final),{recursive:true});
 if(await fs.stat(final).catch(()=>null))throw new Error('Move existing final output before generating a revision.');
 const result=await finalizePresentation({workspaceDir:HERE,candidatePath:mediaCandidate,finalPath:final,pythonExecutable:PYTHON,integrityValidatorPath:path.join(SKILL,'container_tools/inspect_presentation_package_integrity.py'),layoutValidatorPath:path.join(SKILL,'container_tools/inspect_presentation_layout_geometry.py'),layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-heading-fit','--require-native-table-slide','7','--require-native-table-slide','8'],requiredNativeTableOwnerSlides:[7,8],requiredNativeChartOwnerSlides:[6],materializeLiteralChartWorkbooks:true,fontPolicy:{basis:'design',families:[family]},verifyArtifactToolImport:true,receiptPath:path.join(BUILD,'validation.json')});
 await fs.copyFile(final,path.join(HERE,'ATC-Benchmark-Luna-vs-GLM.pptx'));
 console.log(JSON.stringify(result,null,2));
}
console.log(`Rendered ${presentation.slides.items.length} slides. ${previewOnly?'Draft preview only.':'Final deck ready.'}`);
