/** Verify the folder-picker viewer against small local pack-input and run fixtures from file://. */
import assert from 'node:assert/strict';
import {mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {chromium} from 'playwright';

const viewer=resolve(process.argv[2]||'scripts/offline_postprocess/dataset_viewer_picker.html');
const report={passed:false,checks:[],javascript_errors:[],network_requests:[]};
const tmp=await mkdtemp(join(tmpdir(),'viewer-picker-'));
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2ZQAAAABJRU5ErkJggg==','base64');
async function writeJson(file,value){await mkdir(dirname(file),{recursive:true});await writeFile(file,JSON.stringify(value,null,2));}
async function writeText(file,value){await mkdir(dirname(file),{recursive:true});await writeFile(file,value);}

const packDir=join(tmp,'pack');
await writeJson(join(packDir,'pack-index.json'),{
 schema:'responsive_ui_pipeline_index_v1',stage:'final',rule_version:'fixture_pack_v1',
 detail_training_selection:{minimum_target_union_coverage:0.2,box_pixel_policy:{maximum_uninformative_box_ratio:0.6}},
 final_layer_projection:{max_layers:5},
 pages:[{page_id:'p001',status:'valid',url:'https://example.com/pack',title:'Pack fixture',
  viewports:{desktop:{image:'pages/p001/desktop/full.png',hierarchy:'pages/p001/desktop/hierarchy.json',hierarchy_side:'original'}}}]
});
await writeJson(join(packDir,'pages/p001/desktop/hierarchy.json'),{
 schema:'gui_processed_page_v1',rule_version:'fixture_pack_v1',capture:{width:100,height:200},
 split_supervision_eligible:true,split_label:{cut_lines_original_px:[80]},
 detail_training_crop_ids:['p001/desktop/0000'],discarded_detail_crop_ids:[],
 nodes:[
  {global_node_id:'p001/desktop/n1',bbox:[0,0,100,20],struct:'region',subtype:'header',detection_eligible:true,derived_depth:0},
  {global_node_id:'p001/desktop/n2',bbox:[10,30,40,50],struct:'control',subtype:'button',detection_eligible:true,derived_depth:1}
 ],
 crops:[{crop_id:'p001/desktop/0000',core_bbox:[0,0,100,80],crop_bbox:[0,0,100,100],status:'accepted',reason_codes:[],
  targets:[{global_node_id:'p001/desktop/n1',bbox:[0,0,100,20],struct:'region',subtype:'header',derived_depth:0}],
  ignore_regions:[],target_union_coverage:0.5,detail_training_eligible:true,detail_training_discarded:false,
  detail_training_reason_codes:[],box_pixel_evidence:{uninformative_boxes:0,total_boxes:1,uninformative_box_ratio:0},crop_pixel_valid:true}]
});
await writeText(join(packDir,'pages/p001/desktop/full.png'),png);

const dataDir=join(tmp,'dataset'),runDir=join(tmp,'run');
await writeJson(join(dataDir,'collection-index.json'),{schema:'curlie_page_target_v1',batches:[{pages:[
 {id:'p002',url:'https://example.org/run',title:'Run fixture',viewports:{desktop:{}}}
]}]});
await writeJson(join(dataDir,'pages/p002/desktop/hierarchy.json'),{nodes:[
 {node_id:'n1',bbox:[0,0,100,20],struct:'region',disposition:'detection'},
 {node_id:'n2',bbox:[10,30,30,20],struct:'control',disposition:'detection'}
]});
await writeText(join(dataDir,'pages/p002/desktop/full.png'),png);
const runObservation=join(runDir,'observations/p002/desktop');
await writeJson(join(runObservation,'hierarchy.json'),{nodes:[
 {global_node_id:'p002/desktop/n1',bbox:[0,0,100,20],struct:'region',subtype:'header',detection_eligible:true,derived_depth:0},
 {global_node_id:'p002/desktop/n2',bbox:[10,30,40,50],struct:'control',subtype:'button',detection_eligible:true,derived_depth:1}
]});
await writeJson(join(runObservation,'result.json'),{quality:{original_size:{width:100,height:200}},split_label:{cut_lines_original_px:[80]},crops:[
 {crop_id:'p002/desktop/0000',crop_bbox:[0,0,100,100],core_bbox:[0,0,100,80],status:'accepted',reason_codes:[],
  targets:[
   {global_node_id:'p002/desktop/n1',bbox:[0,0,100,20],struct:'region',subtype:'header',derived_depth:0,geometric_visibility:'complete',duplicate_context_occurrence:false},
   {global_node_id:'p002/desktop/n2',bbox:[10,30,40,50],struct:'control',subtype:'button',derived_depth:1,geometric_visibility:'complete',duplicate_context_occurrence:false}
  ],ignore_regions:[]},
 {crop_id:'p002/desktop/0001',crop_bbox:[0,50,100,150],core_bbox:[0,80,100,150],status:'accepted',reason_codes:[],
  targets:[
   {global_node_id:'p002/desktop/n1',bbox:[0,0,100,20],struct:'region',subtype:'header',derived_depth:0,geometric_visibility:'complete',duplicate_context_occurrence:true},
   {global_node_id:'p002/desktop/n2',bbox:[10,30,40,50],struct:'control',subtype:'button',derived_depth:1,geometric_visibility:'partial'}
  ],ignore_regions:[]}
]});
await writeText(join(runDir,'crops/manifest.jsonl'),JSON.stringify({crop_id:'p002/desktop/0000',target_union_coverage:0.5,
 detail_training_eligible:true,detail_training_discarded:false,detail_training_reason_codes:[],
 box_pixel_evidence:{uninformative_boxes:0,total_boxes:2,uninformative_box_ratio:0}})+'\n');
await writeJson(join(runDir,'reports/layer-statistics.json'),{
 layers:[{layer:1,global_detection_targets:1,candidate_unique_targets:1,detail_training_unique_targets:1,detail_training_occurrences:1,withheld_only_by_detail_selection:0},
         {layer:2,global_detection_targets:1,candidate_unique_targets:1,detail_training_unique_targets:1,detail_training_occurrences:1,withheld_only_by_detail_selection:0}],
 totals:{global_detection_targets:2,candidate_unique_targets:2,detail_training_unique_targets:2,detail_training_occurrences:2,withheld_only_by_detail_selection:0},
 source_layers:[{display_layer:'L1',source_layer:0,source_detection_candidates:2}]
});
await writeJson(join(runDir,'index-final.json'),{
 schema:'gui_component_dataset_index_v1',publication_status:'validated',rule_version:'fixture_run_v1',
 final_layer_projection:{max_layers:5},statuses:{accepted:1,rejected:0,review:0},
 detail_training_selection:{minimum_target_union_coverage:0.2},
 observations:[{observation_id:'p002/desktop',page_id:'p002',viewport:'desktop',status:'accepted',reason_codes:[],
  audit:join(runObservation,'result.json'),hierarchy:join(runObservation,'hierarchy.json'),
  source_hierarchy:join(dataDir,'pages/p002/desktop/hierarchy.json'),
  source_image:join(dataDir,'pages/p002/desktop/full.png'),
  original_size:{width:100,height:200},published_crops:1,discarded_detail_crops:0,split_supervision_eligible:true}],
 artifacts:{detail_crop_manifest:{path:join(runDir,'crops/manifest.jsonl')}}
});

let browser,page;
try{
 browser=await chromium.launch({headless:true});
 const context=await browser.newContext({viewport:{width:1480,height:1060}});
 page=await context.newPage();
 page.on('pageerror',error=>report.javascript_errors.push(error.message));
 page.on('request',request=>{if(/^https?:/.test(request.url()))report.network_requests.push(request.url());});
 await page.goto(pathToFileURL(viewer).href,{waitUntil:'load'});
 const articleText=()=>page.locator('article').first().innerText();
 const waitLoaded=async()=>{
  await page.waitForFunction(()=>document.querySelector('#loadStatus').textContent.includes('层级信息已就绪'),null,{timeout:60000});
  await page.waitForFunction(()=>document.querySelectorAll('article').length>0,null,{timeout:60000});
 };

 await page.locator('#folderInput').setInputFiles(packDir);
 await page.waitForFunction(()=>document.querySelector('#datasetInfo').textContent.includes('pack-input'));
 await waitLoaded();
 assert.equal(await page.locator('article').count(),1);
 assert.match(await articleText(),/p001\/desktop/);
 assert.match(await articleText(),/细节训练保留 1/);
 assert.match(await articleText(),/整页切线监督 可用/);
 assert.equal(await page.locator('article canvas').count()>=2,true);
 assert.equal(await page.getByText('此样本读取失败',{exact:false}).count(),0);
 await page.evaluate(()=>{document.querySelector('#layerStats').open=true;});
 assert.match(await page.locator('#layerStatsContent').innerText(),/派生的逐层整页检测框汇总/);
 await page.locator('#mode').selectOption('source');
 await page.waitForFunction(()=>document.querySelector('article').innerText.includes('此目录不含原始 source 标注'));
 report.checks.push('pack-input auto-detection, layers, crops and explicit source-unavailable state');

 await page.locator('#clearFolders').click();
 await page.locator('#folderInput').setInputFiles(runDir);
 await page.waitForFunction(()=>document.querySelector('#loadStatus').textContent.includes('警告'));
 await page.locator('#folderInput').setInputFiles(dataDir);
 await page.waitForFunction(()=>document.querySelector('#loadStatus').textContent.includes('所需文件已全部找到'),null,{timeout:60000});
 await waitLoaded();
 assert.equal(await page.locator('article').count(),1);
 assert.match(await articleText(),/p002\/desktop/);
 assert.match(await articleText(),/源候选 2/);
 assert.match(await articleText(),/覆盖率 50.00%/);
 assert.match(await articleText(),/partial 1/);
 assert.match(await articleText(),/重复完整 1/);
 assert.equal(await page.getByText('此样本读取失败',{exact:false}).count(),0);
 await page.evaluate(()=>{document.querySelector('#layerStats').open=true;});
 assert.match(await page.locator('#layerStatsContent').innerText(),/整页检测去重框/);
 await page.waitForFunction(()=>!document.querySelector('#layers input').disabled);
 await page.locator('#mode').selectOption('final');
 await page.waitForFunction(()=>document.querySelector('article')?.dataset.wholeBoxes==='2');
 await page.evaluate(()=>{const inputs=[...document.querySelectorAll('#layers input')];for(const input of inputs)input.checked=input.value==='1';inputs[0].dispatchEvent(new Event('change',{bubbles:true}));});
 await page.waitForFunction(()=>document.querySelector('article')?.dataset.wholeBoxes==='1');
 report.checks.push('run result + dataset folders resolve absolute references and source/final layer filtering');

 assert.deepEqual(report.javascript_errors,[]);
 assert.deepEqual(report.network_requests,[]);
 report.passed=true;report.browser=browser.version();
} catch(error){
 report.error=error.stack||String(error);process.exitCode=1;
} finally {
 if(page)await page.close();if(browser)await browser.close();
 await mkdir('outputs/dataset-viewer',{recursive:true});
 await writeFile('outputs/dataset-viewer/picker-verification.json',JSON.stringify(report,null,2)+'\n');
 await rm(tmp,{recursive:true,force:true});
 console.log(JSON.stringify(report));
}
