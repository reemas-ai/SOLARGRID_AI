const DEFAULT_HORIZON_MINUTES = 120;
import {api,post} from './api.js';
import {loadMap,loadMiniMap,startMapPolling} from './map.js';

const $=s=>document.querySelector(s);
let latest=null;
let latestAssessment=null;
let latestDynamicPricing=null;
const titles={overview:'Grid Overview',map:'Jordan Solar Grid Map',state:'Grid State Assessment',plans:'Operational Plans',monitoring:'Monitoring & Diagnosis',runs:'Agent Runs',memory:'Memory / Lessons'};
const subtitles={overview:'Real-time insights and AI-powered planning for a reliable and sustainable Jordan grid.',map:'Solar-plant display metadata with dataset-driven operational state.',state:'Backend-owned engineering assessment from Tools 01–04.',plans:'Human-approved planning, validation and execution lifecycle.',monitoring:'Operational change monitoring and outcome diagnosis.',runs:'Auditable agent and tool-call observability.',memory:'Persisted historical cases and reusable operational lessons.'};


const toolCatalog={
  get_system_state:{number:'01',name:'Read Current Grid State',purpose:'Creates a fresh snapshot of demand, generation, battery state and grid condition.'},
  calculate_reserve:{number:'02',name:'Calculate Available Reserve',purpose:'Checks how much upward operating reserve is available for the current snapshot.'},
  detect_imbalance:{number:'03',name:'Detect Supply–Demand Imbalance',purpose:'Detects the current generation-versus-demand imbalance and its severity.'},
  assess_future_risk:{number:'04',name:'Assess Near-Term Grid Risk',purpose:'Uses forecasts to identify risk over the next operating horizon.'},
  generate_and_optimize_plans:{number:'05',name:'Generate Response Plans',purpose:'Builds candidate operating plans from the verified grid state.'},
  check_generator_constraints:{number:'06',name:'Validate Generator Limits',purpose:'Checks generator capacity, ramping and availability constraints.'},
  check_battery_constraints:{number:'07',name:'Validate Battery Limits',purpose:'Checks battery SOC, charge/discharge and energy constraints.'},
  run_power_flow:{number:'08',name:'Validate Network Power Flow',purpose:'Checks whether the candidate plan is feasible on the transmission network.'},
  evaluate_plans:{number:'09',name:'Evaluate Candidate Plans',purpose:'Evaluates feasible candidate plans using the configured decision policy.'},
  run_scenario_analysis:{number:'10',name:'Run What-If Scenario',purpose:'Tests a hypothetical operating change without altering the live demo state.'},
  monitor_system_conditions:{number:'11',name:'Check Operating Changes',purpose:'Compares the approved-plan baseline with the latest grid snapshot.'},
  assess_change_impact:{number:'12',name:'Recheck Plan Validity',purpose:'Determines whether new conditions affect the approved plan or require replanning.'},
  execute_and_verify_plan:{number:'13',name:'Execute & Verify Approved Plan',purpose:'Applies the approved plan in simulation and records the executed actions.'},
  assess_and_diagnose_outcome:{number:'14',name:'Diagnose Post-Execution Outcome',purpose:'Compares the execution with the post-execution grid state and records the outcome.'},
  forecast_error_analysis:{number:'15',name:'Analyze Forecast Errors',purpose:'Compares forecasts with actual measurements and identifies evidence-backed patterns.'},
  retrieve_engineering_evidence:{number:'16',name:'Retrieve Engineering Evidence',purpose:'Retrieves traceable engineering guidance from the project RAG knowledge base.'},
  analyze_dynamic_pricing:{number:'17',name:'Analyze Dynamic Pricing',purpose:'Calculates a solar-only price signal and conserved flexible-load shift from authoritative forecasts and asset state.'}
};
const runLabels={
  STATE_ASSESSMENT:'Grid State Assessment',PLAN_GENERATION:'Plan Generation & Validation',DYNAMIC_PRICING_ANALYSIS:'Dynamic Pricing Analysis',DYNAMIC_PRICING_PLAN_GENERATION:'Dynamic Pricing Plan Generation',PLAN_MONITORING:'Plan Change Check',PLAN_REVALIDATION:'Plan Recheck',PLAN_EXECUTION:'Approved Plan Execution',OUTCOME_DIAGNOSIS:'Outcome Diagnosis',USER_PROMPT:'Operator Request',SCHEDULED:'Scheduled Monitor Cycle',AUTONOMOUS_MONITOR:'Autonomous Monitor Cycle'
};
const runLabel=v=>runLabels[String(v||'').toUpperCase()]||String(v||'System Operation').replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());
const toolMeta=name=>toolCatalog[name]||{number:'—',name:String(name||'Unknown tool').replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase()),purpose:'Backend operation recorded for audit.'};

const fmt=(v,d=1)=>v==null||Number.isNaN(Number(v))?'—':Number(v).toFixed(d);
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const toolData=v=>v&&typeof v==='object'&&v.data&&typeof v.data==='object'?v.data:(v||{});
const statusClass=v=>{const s=String(v||'UNKNOWN').toUpperCase();if(['OK','STABLE','SUFFICIENT','LOW','BALANCED','SUCCESS','FEASIBLE','APPROVED','NORMAL','GREEN','EXECUTED','ALLOW','ACHIEVED','PLANS_EVALUATED'].includes(s))return'good';if(['WARNING','MEDIUM','PARTIAL','PROPOSED','YELLOW','DEGRADED','UNKNOWN','NOT_VALIDATED','NEEDS_VALIDATION','STARTED','EXECUTED_PENDING_DIAGNOSIS','PENDING','VERIFICATION_PENDING','NO_ACTION','PARTIAL_EVIDENCE','NOT_EVALUATED'].includes(s))return'warn';if(['CRITICAL','HIGH','INSUFFICIENT','FAILED','REJECTED','INFEASIBLE','RED','PROBLEM','BLOCKED_BY_SAFETY','EXECUTION_FAILED','NOT_ACHIEVED','ANALYSIS_INCOMPLETE'].includes(s))return'bad';return'neutral'};
function toast(m,duration=2600){const t=$('#toast');if(!t)return;t.textContent=m;t.classList.add('show');clearTimeout(t._hideTimer);t._hideTimer=setTimeout(()=>t.classList.remove('show'),duration)}
function val(v,u=''){return v==null?'—':`${fmt(v)}${u?` ${u}`:''}`}
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function settleAfterMinimum(taskPromise,minMs){const started=Date.now();try{const value=await taskPromise;const remaining=Math.max(0,minMs-(Date.now()-started));if(remaining)await delay(remaining);return value}catch(error){const remaining=Math.max(0,minMs-(Date.now()-started));if(remaining)await delay(remaining);throw error}}

function view(id){document.querySelectorAll('.view').forEach(x=>x.classList.toggle('active',x.id===id));document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.view===id));$('#title').textContent=titles[id]||id;$('#subtitle').textContent=subtitles[id]||'';if(id==='map'){loadMap();startMapPolling()}if(id==='overview')loadMiniMap();if(id==='plans')loadPlans();if(id==='runs')loadRuns();if(id==='memory')loadMemory()}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>view(b.dataset.view));
document.querySelectorAll('[data-view-jump]').forEach(b=>b.onclick=()=>view(b.dataset.viewJump));

function updateClock(){const el=$('#clock');if(el)el.textContent=new Date().toLocaleString(undefined,{weekday:'short',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}
updateClock();setInterval(updateClock,30000);

async function health(){try{await api('/api/health');$('#apiStatus').textContent='Online'}catch{$('#apiStatus').textContent='Offline'}}

function setTag(el,value){if(!el)return;el.textContent=value||'Unknown';el.className=`status-tag ${statusClass(value)}`}

function renderSnapshot(s,reserve=null){
  const previousSnapshotId=latest?.id;
  latest=s;
  if(!s)return;
  if(previousSnapshotId!=null&&Number(previousSnapshotId)!==Number(s.id)){
    latestDynamicPricing=null;
    renderDynamicPricing(null);
  }
  $('#mDemand').textContent=fmt(s.demand_mw);$('#mGeneration').textContent=fmt(s.total_generation_mw??s.solar_gen_mw??0);$('#mReserve').textContent=fmt(reserve?.reserve_margin_mw??s.reserve_margin_mw);$('#mSoc').textContent=fmt(s.battery_soc_pct);
  $('#bannerDemand').textContent=val(s.demand_mw,'MW');$('#bannerReserve').textContent=val(reserve?.reserve_margin_mw??s.reserve_margin_mw,'MW');$('#bannerUpdated').textContent=s.timestamp?new Date(s.timestamp).toLocaleTimeString():'—';
  const st=(s.grid_status||'UNKNOWN').toUpperCase();$('#systemStatus').textContent=st==='STABLE'?'Jordan grid is stable':`Grid condition: ${st}`;$('#systemSub').textContent=`Snapshot #${s.id} · ${s.data_source||'Unknown source'}`;$('#systemBanner').dataset.status=statusClass(st);
  $('#healthSolar').textContent=val(s.solar_gen_mw,'MW');$('#healthSolarFleet').textContent=val(s.solar_gen_mw,'MW');
}

function renderAssessment(result){latestAssessment=result;const st=toolData(result?.state),rv=toolData(result?.reserve),im=toolData(result?.imbalance),rk=toolData(result?.future_risk);const box=$('#stateResult');const reserveActual=rv.actual_reserve_mw;const reserveRequired=rv.required_reserve_mw;const reserveMargin=rv.reserve_margin_mw;const reserveMeta=reserveActual!=null&&reserveRequired!=null?`Required ${val(reserveRequired,'MW')} · Margin ${val(reserveMargin,'MW')} · ${esc(rv.reserve_status||'UNKNOWN')}`:esc(rv.reserve_status||'UNKNOWN');if(box){box.className='assessment-dashboard';box.innerHTML=`<div class="assessment-hero"><div><span class="eyebrow">CURRENT OPERATING PICTURE</span><h3>System assessment</h3><p>Snapshot #${esc(result?.snapshot_id??st.snapshot_id??'—')} · ${esc(st.data_quality||'Unknown quality')}</p></div><span class="status-pill ${statusClass(st.grid_status)}">${esc(st.grid_status||'UNKNOWN')}</span></div><div class="source-context-strip"><span class="badge info">Source: ${esc(st.data_source||'SIMULATION')}</span><span class="badge ${statusClass(st.external_data_status)}">External context: ${esc(String(st.external_data_status||'NOT USED').replaceAll('_',' '))}</span>${st.external_data_retrieved_at?`<span class="badge">Retrieved ${esc(new Date(st.external_data_retrieved_at).toLocaleString())}</span>`:''}${st.external_data_location?`<span class="badge">${esc(st.external_data_location)}</span>`:''}<span class="badge warn">Grid telemetry: SYNTHETIC DEMO</span></div><div class="assessment-grid"><div class="assessment-card"><span>Demand</span><strong>${val(st.demand_mw,'MW')}</strong><small>Current load</small></div><div class="assessment-card"><span>Total generation</span><strong>${val(st.total_generation_mw??st.solar_gen_mw,'MW')}</strong><small>Solar generation only</small></div><div class="assessment-card"><span>Solar</span><strong>${val(st.solar_gen_mw,'MW')}</strong><small>Solar generation in current state</small></div><div class="assessment-card"><span>Solar-only system</span><strong>100%</strong><small>All generation assets are solar</small></div><div class="assessment-card"><span>Available reserve</span><strong>${val(reserveActual,'MW')}</strong><small>${reserveMeta}</small></div><div class="assessment-card"><span>Future risk</span><strong>${esc(rk.max_severity||'UNKNOWN')}</strong><small>${esc(rk.horizon_minutes||'—')} min horizon</small></div></div><details class="technical-details"><summary>View technical payload</summary><pre>${esc(JSON.stringify(result,null,2))}</pre></details>`}
  $('#healthBalance').textContent=val(im.imbalance_mw,'MW');setTag($('#healthBalanceStatus'),im.severity||im.direction);$('#healthReserve').textContent=val(reserveMargin,'MW');setTag($('#healthReserveStatus'),rv.reserve_status);$('#healthRisk').textContent=rk.max_severity||'—';setTag($('#healthRiskStatus'),rk.max_severity);
}

function renderExternalSources(sources=[]){const box=$('#externalSources');if(!box)return;box.innerHTML='';if(!sources.length){box.innerHTML='<div class="external-source-card"><span>Status</span><b>Not checked yet</b><small>No external-source provenance has been stored.</small></div>';return}sources.forEach(src=>{const status=String(src.status||'NOT_CHECKED').toUpperCase(),when=src.retrieved_at?new Date(src.retrieved_at).toLocaleString():'Not retrieved',vars=(src.variables||[]).slice(0,4).join(', ')||'No variables';const el=document.createElement('div');el.className='external-source-card';el.innerHTML=`<span>${esc(src.source||'External source')}</span><b class="source-status-${esc(status)}">${esc(status.replaceAll('_',' '))}</b><small>${esc(src.source_role||'External reference')} · ${esc(when)}</small><div class="source-meta"><span class="badge">${esc(src.location||'Location/reference')}</span><span class="badge">${esc(vars)}</span></div>${src.error?`<small>${esc(src.error)}</small>`:''}`;box.append(el)})}
async function loadSummary(){try{const d=await api('/api/dashboard/summary');renderSnapshot(d.snapshot,d.reserve);renderExternalSources(d.external_sources||[]);if(d.snapshot){const lines=d.snapshot.state_json?.grid_lines||[];const congested=lines.filter(x=>x.is_congested).length;$('#healthCongestion').textContent=congested?`${congested} area${congested>1?'s':''}`:'None';setTag($('#healthCongestionStatus'),congested?'WARNING':'LOW')}if(d.latest_lesson)$('#autoLesson').textContent=`Lesson #${d.latest_lesson.id}`;else $('#autoLesson').textContent='None';}catch(e){toast(e.message)}}

async function loadMonitorStatus(){try{const d=await api('/api/monitor/status'),m=d.monitor||{};$('#autoRunning').textContent=m.running?'Active':'Idle';$('#autoMode').textContent=String(m.mode||'—').replaceAll('_',' ');$('#autoInterval').textContent=m.poll_interval_seconds?`${Math.round(Number(m.poll_interval_seconds)/60)} min`:'—';$('#autoLastCheck').textContent=m.last_check?new Date(m.last_check).toLocaleTimeString():'—';$('#autoNextCheck').textContent=m.next_check?new Date(m.next_check).toLocaleTimeString():'—';$('#autoSnapshot').textContent=m.last_snapshot_id?`#${m.last_snapshot_id}`:'—';$('#autoCondition').textContent=m.last_condition||'—';$('#autoEvent').textContent=m.latest_event?.event_type?String(m.latest_event.event_type).replaceAll('_',' '):'None';$('#autoPlan').textContent=m.latest_plan_id?`#${m.latest_plan_id}`:'None';}catch{}}

async function assess(){const bs=[...document.querySelectorAll('[data-action="assess"]')];try{bs.forEach(b=>{b.disabled=true;b.dataset.t=b.textContent;b.textContent='Assessing…'});const d=await post('/api/state/assess',{horizon_minutes:DEFAULT_HORIZON_MINUTES},{timeoutMs:90000});renderAssessment(d.result);await loadSummary();toast('Assessment completed')}catch(e){toast(e.message)}finally{bs.forEach(b=>{b.disabled=false;b.textContent=b.dataset.t||'Run Assessment'})}}
document.querySelectorAll('[data-action="assess"]').forEach(b=>b.onclick=assess);

function planLabel(p){return p.plan_name||`Plan #${p.id}`}

const operatorLabels={
  solar_forecast:'Solar forecast',
  demand_forecast:'Demand forecast',
  battery_soc_pct:'Battery charge',
  generator_availability:'Generator availability',
  forecast_revision:'Forecast update',
  solar_generation:'Solar generation',
  demand:'Demand',
  grid_status:'Grid condition',
  reserve_margin_mw:'Reserve margin'
};
function operatorLabel(value){
  const key=String(value||'').toLowerCase();
  return operatorLabels[key]||String(value||'Operational indicator').replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());
}
function operatorValue(variable,value){
  if(value==null)return 'Not available';
  if(typeof value==='object')return 'Updated';
  if(typeof value==='boolean')return value?'Yes':'No';
  if(typeof value==='number'){
    if(String(variable).includes('soc'))return `${fmt(value,1)} %`;
    if(String(variable).includes('forecast')||String(variable).includes('generation')||String(variable).includes('demand')||String(variable).includes('reserve')||String(variable).includes('dispatch'))return `${fmt(value,1)} MW`;
    return fmt(value,1);
  }
  return String(value).replaceAll('_',' ');
}
function technicalDetails(result){
  return `<details class="technical-details"><summary><i class="fa-solid fa-code"></i> Technical details (engineering / IT)</summary><pre>${esc(JSON.stringify(result,null,2))}</pre></details>`;
}
function monitorMetric(label,value,help='',cls=''){
  return `<div class="operator-metric"><span>${esc(label)}</span><strong class="${cls}">${esc(value)}</strong>${help?`<small>${esc(help)}</small>`:''}</div>`;
}
function changeRows(changes=[]){
  if(!changes.length)return '<div class="operator-empty"><i class="fa-solid fa-circle-check"></i><div><b>No material operating changes were recorded</b><span>The monitored values remain within the configured thresholds.</span></div></div>';
  return `<div class="change-list">${changes.map(c=>{
    const crossed=c.threshold_crossed===true;
    const unknown=c.threshold_crossed==null;
    const state=crossed?'bad':unknown?'neutral':'good';
    const tag=crossed?'Attention':unknown?'Not enough data':'Within limit';
    const magnitude=typeof c.magnitude==='number'?`${fmt(c.magnitude,1)}${String(c.variable).includes('battery_soc')?' pts':' %'}`:'—';
    return `<div class="change-row">
      <div class="change-icon ${state}"><i class="fa-solid ${crossed?'fa-triangle-exclamation':unknown?'fa-circle-question':'fa-circle-check'}"></i></div>
      <div class="change-main"><b>${esc(operatorLabel(c.variable))}</b><span>${esc(operatorValue(c.variable,c.old_value))} <i class="fa-solid fa-arrow-right"></i> ${esc(operatorValue(c.variable,c.new_value))}</span></div>
      <div class="change-magnitude"><small>Change</small><b>${esc(magnitude)}</b></div>
      <span class="badge ${state}">${tag}</span>
    </div>`;
  }).join('')}</div>`;
}
function plannedActionRows(changes=[]){
  if(!changes.length)return '<div class="operator-empty"><i class="fa-solid fa-circle-info"></i><div><b>No first-interval dispatch delta available</b><span>Review the technical plan details for this strategy.</span></div></div>';
  return `<div class="change-list">${changes.map(c=>{
    const delta=typeof c.delta_mw==='number'?`${c.delta_mw>=0?'+':''}${fmt(c.delta_mw,1)} MW`:'—';
    const pct=typeof c.change_pct==='number'?` (${c.change_pct>=0?'+':''}${fmt(c.change_pct,1)}%)`:'';
    return `<div class="change-row">
      <div class="change-icon neutral"><i class="fa-solid fa-arrow-right-arrow-left"></i></div>
      <div class="change-main"><b>${esc(operatorLabel(c.variable))}</b><span>${esc(operatorValue(c.variable,c.old_value))} <i class="fa-solid fa-arrow-right"></i> ${esc(operatorValue(c.variable,c.new_value))}</span></div>
      <div class="change-magnitude"><small>Requested delta</small><b>${esc(delta+pct)}</b></div>
      <span class="badge neutral">Planned</span>
    </div>`;
  }).join('')}</div>`;
}
function renderMonitorCheck(title,result){
  const envelope=result?.monitoring||result||{};
  const d=toolData(envelope);
  const changed=d.change_detected===true;
  const sameSnapshot=d.comparison_context?.same_snapshot===true;
  const evidence=String(d.forecast_evidence_status||'UNKNOWN').toUpperCase();
  const trigger=d.trigger_reason?operatorLabel(d.trigger_reason):'None';
  const headline=sameSnapshot?'Waiting for fresh telemetry':changed?'Operational change detected':'No material change detected';
  const guidance=sameSnapshot
    ?'No new grid observation has arrived since this plan was approved. Change detection is waiting for fresh telemetry.'
    :changed
      ?'Review the highlighted changes and recheck the plan before execution.'
      :'No operator action is required from this monitoring check.';
  const cls=sameSnapshot?'neutral':changed?'warn':'good';
  const box=$('#monitorResult');
  box.className='monitor-dashboard';
  box.innerHTML=`
    <div class="operator-hero ${cls}">
      <div class="operator-hero-icon"><i class="fa-solid ${changed?'fa-triangle-exclamation':'fa-shield-circle-check'}"></i></div>
      <div><span class="eyebrow">PLAN CHANGE CHECK</span><h3>${esc(headline)}</h3><p>${esc(guidance)}</p></div>
      <span class="status-pill ${cls}">${sameSnapshot?'WAITING FOR TELEMETRY':changed?'REVIEW':'EVALUATED'}</span>
    </div>
    <div class="monitor-summary operator-summary">
      ${monitorMetric('Plan',`#${d.affected_plan_id??'—'}`,'Approved plan being monitored')}
      ${monitorMetric('Plan baseline',`Snapshot #${d.comparison_context?.baseline_snapshot_id??'—'}`,'Snapshot used when this plan was created')}
      ${monitorMetric('Latest grid observation',`Snapshot #${d.comparison_context?.current_snapshot_id??'—'}`,'Newest snapshot currently available')}
      ${monitorMetric('Fresh telemetry',sameSnapshot?'Waiting':'Available',sameSnapshot?'No independent newer observation exists':'A newer independent observation is available',sameSnapshot?'warn':'good')}
      ${monitorMetric('Change detected',sameSnapshot?'Not evaluated':changed?'Yes':'No',sameSnapshot?'A newer snapshot is required for an observed-change comparison':changed?'At least one configured threshold was crossed':'All observed changes are within limits',cls)}
      ${monitorMetric('Forecast evidence',sameSnapshot?'Same snapshot':evidence==='AVAILABLE'?'Available':'Limited',sameSnapshot?'No independent newer forecast snapshot exists':evidence==='AVAILABLE'?'Forecast values were compared':'Some forecast comparison evidence is unavailable',sameSnapshot?'neutral':evidence==='AVAILABLE'?'good':'warn')}
      ${monitorMetric('Main trigger',trigger,changed?'Reason that triggered attention':'No threshold trigger')}
    </div>
    <div class="operator-section">
      <div class="operator-section-head"><div><span class="eyebrow">WHAT CHANGED</span><h4>Operating changes</h4></div><span class="operator-event">Monitoring event #${esc(d.monitoring_event_id??'—')}</span></div>
      ${sameSnapshot?`<div class="operator-empty"><i class="fa-solid fa-clock-rotate-left"></i><div><b>Baseline and current snapshot are both #${esc(d.comparison_context?.current_snapshot_id??'—')}</b><span>The previous 0% values were a same-snapshot comparison, not evidence that execution makes no changes.</span></div></div>`:changeRows(d.changes||[])}
    </div>
    ${diagnosis.dynamic_pricing_outcome?`<div class="operator-section"><div class="operator-section-head"><div><span class="eyebrow">DYNAMIC PRICING OUTCOME</span><h4>Expected vs actual impact</h4></div><span class="status-pill ${statusClass(diagnosis.dynamic_pricing_outcome.goal_result)}">${esc(String(diagnosis.dynamic_pricing_outcome.goal_result||'NOT_EVALUATED').replaceAll('_',' '))}</span></div><div class="monitor-summary operator-summary">${monitorMetric('Load shift',diagnosis.dynamic_pricing_outcome.actual_load_shift_mw==null?'Not available':`${fmt(diagnosis.dynamic_pricing_outcome.actual_load_shift_mw,2)} MW`,diagnosis.dynamic_pricing_outcome.expected_load_shift_mw==null?'Expected unavailable':`Expected ${fmt(diagnosis.dynamic_pricing_outcome.expected_load_shift_mw,2)} MW`)}${monitorMetric('Surplus reduction',diagnosis.dynamic_pricing_outcome.actual_surplus_reduction_mw==null?'Not available':`${fmt(diagnosis.dynamic_pricing_outcome.actual_surplus_reduction_mw,2)} MW`,diagnosis.dynamic_pricing_outcome.expected_surplus_reduction_mw==null?'Expected unavailable':`Expected ${fmt(diagnosis.dynamic_pricing_outcome.expected_surplus_reduction_mw,2)} MW`)}${monitorMetric('Curtailment reduction',diagnosis.dynamic_pricing_outcome.actual_curtailment_reduction_mw==null?'Not available':`${fmt(diagnosis.dynamic_pricing_outcome.actual_curtailment_reduction_mw,2)} MW`,diagnosis.dynamic_pricing_outcome.expected_curtailment_reduction_mw==null?'Expected unavailable':`Expected ${fmt(diagnosis.dynamic_pricing_outcome.expected_curtailment_reduction_mw,2)} MW`)}${monitorMetric('Solar utilization gain',diagnosis.dynamic_pricing_outcome.actual_utilization_improvement_ratio==null?'Not available':`${fmt(diagnosis.dynamic_pricing_outcome.actual_utilization_improvement_ratio*100,2)}%`,diagnosis.dynamic_pricing_outcome.expected_utilization_improvement_ratio==null?'Expected unavailable':`Expected ${fmt(diagnosis.dynamic_pricing_outcome.expected_utilization_improvement_ratio*100,2)}%`)}</div>${diagnosis.dynamic_pricing_outcome.lesson_signal?`<div class="operator-guidance warn"><i class="fa-solid fa-brain"></i><div><b>Learning signal</b><p>${esc(diagnosis.dynamic_pricing_outcome.lesson_signal)}</p></div></div>`:''}</div>`:''}
    <div class="operator-guidance ${cls}"><i class="fa-solid fa-user-shield"></i><div><b>Operator guidance</b><p>${esc(guidance)}</p></div></div>
    ${technicalDetails(result)}`;
}
function renderRevalidation(title,result){
  const envelope=result?.impact||result||{};
  const d=toolData(envelope);
  const validation=result?.validation?.evaluation||{};
  const deterministic=validation.is_feasible===true?'FEASIBLE':validation.is_feasible===false?'INFEASIBLE':'UNKNOWN';
  const stillValid=d.still_valid;
  const replan=result?.replan_required===true;
  const impact=String(d.impact_level||'UNKNOWN').toUpperCase();
  const valid=result?.plan_valid===true;
  const validLabel=valid?'Valid':replan?'Replan required':'Needs review';
  const cls=valid?'good':replan?'bad':'warn';
  const sameSnapshot=d.comparison_context?.same_snapshot===true;
  const guidance=sameSnapshot
    ?'Approval does not change the physical grid. No newer snapshot exists yet, so observed changes are zero by design; the planned dispatch changes are shown separately below.'
    :valid
      ?'The plan remains valid against the current snapshot and deterministic engineering checks.'
      :replan
        ?'Do not execute this plan. Generate a replacement plan from the current grid snapshot and obtain a new approval.'
        :'Hold execution until the unresolved validation evidence is refreshed.';
  const evidence=(d.affected_assumptions||[]).filter(x=>x&&x.variable).slice(0,8).map(x=>({
    variable:x.variable,old_value:x.old_value,new_value:x.new_value,magnitude:x.magnitude,threshold_crossed:x.threshold_crossed
  }));
  const planned=(d.planned_action_deltas||[]).slice(0,12);
  const box=$('#monitorResult');
  box.className='monitor-dashboard';
  box.innerHTML=`
    <div class="operator-hero ${cls}">
      <div class="operator-hero-icon"><i class="fa-solid ${valid?'fa-clipboard-check':replan?'fa-ban':'fa-magnifying-glass-chart'}"></i></div>
      <div><span class="eyebrow">PLAN REVALIDATION</span><h3>${esc(validLabel)}</h3><p>${esc(guidance)}</p></div>
      <span class="status-pill ${cls}">${esc(impact)}</span>
    </div>
    <div class="monitor-summary operator-summary">
      ${monitorMetric('Plan',`#${d.plan_id??d.active_plan_id??'—'}`,'Plan checked against the latest snapshot')}
      ${monitorMetric('Change impact',stillValid===true?'Acceptable':stillValid===false?'Invalid':'Needs review','Tool 12 change-impact result',stillValid===true?'good':stillValid===false?'bad':'warn')}
      ${monitorMetric('Deterministic checks',deterministic,validation.rejection_reason||'Tool 09 validation result',statusClass(deterministic))}
      ${monitorMetric('New plan required',replan?'Yes':'No',replan?'Replanning is required before execution':'No replanning requirement',replan?'bad':'good')}
    </div>
    <div class="operator-section">
      <div class="operator-section-head"><div><span class="eyebrow">OBSERVED GRID CHANGE</span><h4>${sameSnapshot?'No newer snapshot yet':'Key operating indicators'}</h4></div><span class="operator-event">Assessment #${esc(d.assessment_id??'—')}</span></div>
      ${sameSnapshot?`<div class="operator-empty"><i class="fa-solid fa-circle-info"></i><div><b>Baseline and current snapshot are both #${esc(d.comparison_context?.current_snapshot_id??'—')}</b><span>0% here means no new observation has arrived; it does not mean the approved plan has no actions.</span></div></div>`:changeRows(evidence)}
    </div>
    <div class="operator-section">
      <div class="operator-section-head"><div><span class="eyebrow">PLANNED DISPATCH CHANGE</span><h4>What execution will request in the first interval</h4></div></div>
      ${plannedActionRows(planned)}
    </div>
    <div class="operator-guidance ${cls}"><i class="fa-solid fa-user-shield"></i><div><b>Operator guidance</b><p>${esc(guidance)}</p></div></div>
    ${technicalDetails(result)}`;
}
function valueOrNA(v,unit=''){return v==null?'Not available':`${fmt(v,2)}${unit}`}
function passText(v){return v===true?'PASS':v===false?'FAIL':'Not available'}
function beforeAfterTable(bundle={}){
  const labels={demand_mw:'Demand',total_generation_mw:'Total Generation',battery_power_mw:'Battery Power',reserve_margin_mw:'Reserve Margin',residual_imbalance_mw:'Residual Imbalance',battery_soc_pct:'Battery SOC'};
  const units={demand_mw:' MW',total_generation_mw:' MW',battery_power_mw:' MW',reserve_margin_mw:' MW',residual_imbalance_mw:' MW',battery_soc_pct:' %'};
  const rows=bundle.metrics||[];
  if(!rows.length)return '<div class="operator-empty"><i class="fa-solid fa-circle-info"></i><div><b>Before/after evidence is not available</b><span>No values are invented when a snapshot metric is missing.</span></div></div>';
  return `<div class="comparison-table"><div class="comparison-row head"><span>Metric</span><span>Before</span><span>After</span></div>${rows.map(r=>`<div class="comparison-row"><b>${esc(labels[r.key]||operatorLabel(r.key))}</b><span>${esc(valueOrNA(r.before,units[r.key]||''))}</span><span>${esc(valueOrNA(r.after,units[r.key]||''))}</span></div>`).join('')}</div>`;
}
function expectedActualTable(rows=[]){
  if(!rows.length)return '<div class="operator-empty"><i class="fa-solid fa-circle-info"></i><div><b>Command comparison is not available</b><span>Requested or actual action evidence is incomplete.</span></div></div>';
  const trackingText=r=>{
    const state=String(r.comparison||'').toUpperCase();
    const delta=r.delta_mw==null?'':` (${Number(r.delta_mw)>=0?'+':''}${fmt(r.delta_mw,2)} MW)`;
    if(state==='EXACT')return '✓ Exact';
    if(state==='WITHIN_TOLERANCE')return `≈ Within tolerance${delta}`;
    if(state==='OUT_OF_TOLERANCE')return `✕ Outside tolerance${delta}`;
    return r.match===true?`✓ Within tolerance${delta}`:r.match===false?`✕ Outside tolerance${delta}`:'Not available';
  };
  const trackingClass=r=>r.match===true?'good':r.match===false?'bad':'neutral';
  return `<div class="comparison-table expected"><div class="comparison-row head"><span>Asset</span><span>Expected</span><span>Actual</span><span>Tracking</span></div>${rows.map(r=>`<div class="comparison-row"><b>${esc(`${operatorLabel(r.asset_type)} ${r.asset_id} · interval ${r.interval??'—'}`)}</b><span>${esc(valueOrNA(r.expected_mw,' MW'))}</span><span>${esc(valueOrNA(r.actual_mw,' MW'))}</span><span class="${trackingClass(r)}">${esc(trackingText(r))}</span></div>`).join('')}</div>`;
}
function lifecycleStrip({blocked=false,executionStatus='UNKNOWN',goal='NOT_EVALUATED'}={}){
  const stages=[['GENERATED','done'],['VALIDATED','done'],['APPROVED','done'],[blocked?'SAFETY BLOCKED':'SAFETY CLEARED',blocked?'bad':'done']];
  if(!blocked){stages.push([executionStatus==='SUCCESS'?'EXECUTED':'EXECUTION ATTEMPTED',executionStatus==='SUCCESS'?'done':'warn']);if(goal==='ACHIEVED')stages.push(['VERIFIED','done']);else if(goal==='NOT_ACHIEVED'||goal==='PARTIAL')stages.push(['REPLAN REQUIRED','bad']);else stages.push(['NOT EVALUATED','warn'])}
  return `<div class="lifecycle-strip">${stages.map((x,i)=>`${i?'<i class="fa-solid fa-chevron-right"></i>':''}<span class="${x[1]}">${esc(x[0])}</span>`).join('')}</div>`;
}
function replacementRecoverySection(plans,title='REPLACEMENT PLANS'){
  const items=(Array.isArray(plans)?plans:[]).map(p=>{
    const validation=String(p.validation_status||'UNKNOWN').toUpperCase();
    const feasible=validation==='FEASIBLE';
    const approvable=String(p.status||'').toUpperCase()==='PROPOSED'&&feasible;
    return `<article class="panel plan-row"><div class="plan-main"><div class="plan-title-row"><b>${esc(p.plan_name||`Plan #${p.id}`)}</b><span class="badge ${statusClass(p.status)}">${esc(p.status||'UNKNOWN')}</span><span class="badge ${statusClass(validation)}">${esc(validation.replaceAll('_',' '))}</span></div><p>Plan #${esc(p.id)} · Parent Plan #${esc(p.parent_plan_id??'—')} · Snapshot #${esc(p.snapshot_id??'—')}</p><small>${esc(feasible?'Deterministic validation passed. Not automatically approved or executed. Human Approval is required before execution.':validation==='INFEASIBLE'?'Deterministic validation marked this candidate infeasible.':'Validation evidence is incomplete; approval is disabled.')}</small></div><div class="actions">${approvable?`<button class="primary compact recovery-approve" type="button" data-plan-id="${esc(p.id)}">Approve</button>`:validation==='INFEASIBLE'?'<button class="secondary compact" type="button" disabled>Not Executable</button>':''}</div></article>`;
  }).join('');
  return `<div class="operator-section"><div class="operator-section-head"><div><span class="eyebrow">${esc(title)}</span><h4>Generated and deterministically validated</h4></div></div>${items||'<div class="operator-empty"><i class="fa-solid fa-circle-exclamation"></i><div><b>No replacement plan was persisted</b><span>Review the backend planning/validation result before retrying.</span></div></div>'}</div>`;
}
function wireRecoveryApprovalActions(root){
  root.querySelectorAll('.recovery-approve').forEach(button=>{
    button.onclick=async()=>{
      const id=Number(button.dataset.planId);
      button.disabled=true;button.textContent='Approving…';
      try{
        await planAction(id,'approve');
        button.textContent='Approved';
      }catch(e){
        button.disabled=false;button.textContent='Approve';
      }
    };
  });
}
function renderExecution(title,result){
  const safety=result?.safety||{};
  const blocked=String(result?.status||'').toUpperCase()==='BLOCKED_BY_SAFETY'||String(safety.decision||'').toUpperCase()==='BLOCK';
  const execution=toolData(result?.execution||{});
  const diagnosis=toolData(result?.post_execution?.diagnosis||{});
  const executionStatus=blocked?'BLOCKED':String(execution.execution_status||result?.status||'UNKNOWN').toUpperCase();
  const postStatus=String(result?.post_execution?.status||'').toUpperCase();
  const verificationPending=postStatus==='PENDING'||String(result?.status||'').toUpperCase()==='VERIFICATION_PENDING';
  const goal=verificationPending?'PENDING':String(diagnosis.goal_result||'NOT_EVALUATED').toUpperCase();
  const replan=diagnosis.replan_required===true||goal==='NOT_ACHIEVED'||goal==='PARTIAL'||['FAILED','PARTIAL_FAIL'].includes(executionStatus);
  const commandMatch=diagnosis.command_match;
  const commandRows=diagnosis.expected_vs_actual||[];
  const hasNormalVariance=commandMatch===true&&commandRows.some(r=>String(r.comparison||'').toUpperCase()==='WITHIN_TOLERANCE');
  const cls=blocked?'bad':executionStatus==='SUCCESS'?(goal==='ACHIEVED'?'good':'warn'):executionStatus==='PARTIAL_FAIL'?'warn':'bad';
  const guidance=blocked
    ?'Execution was stopped by the safety gate. Review the failed checks before trying again.'
    :verificationPending?'Execution completed, but the operational result cannot be finalized until due verification evidence exists.'
    :goal==='ACHIEVED'?'Execution completed and the post-execution operational goal was achieved.'
    :replan?'Execution completed, but the operational result requires review or replanning.'
    :'Execution completed. Some operational evidence was unavailable, so the goal was not fully evaluated.';
  const checks=(safety.checks||[]).map(c=>`<div class="safety-check"><span class="badge ${statusClass(c.status)}">${esc(c.status||'UNKNOWN')}</span><div><b>${esc(operatorLabel(c.name))}</b><small>${esc(c.message||c.reason_code||'')}</small></div></div>`).join('');
  const memory=result?.post_execution?.memory||{};
  const lesson=memory.lesson_decision||{};
  const timeline=[];
  if(execution.timestamp)timeline.push([execution.timestamp,`Execution #${execution.execution_id||'—'} completed: ${executionStatus}`]);
  if(diagnosis.verification_checked_at)timeline.push([diagnosis.verification_checked_at,`Goal verification completed: ${goal}`]);
  const box=$('#monitorResult');box.className='monitor-dashboard';
  box.innerHTML=`
    <div class="operator-hero ${cls}"><div class="operator-hero-icon"><i class="fa-solid ${blocked?'fa-shield-halved':'fa-bolt'}"></i></div><div><span class="eyebrow">EXECUTION + OPERATIONAL RESULT</span><h3>${esc(blocked?'Execution blocked safely':goal==='ACHIEVED'?'Operational goal achieved':executionStatus==='SUCCESS'?'Execution completed; outcome reviewed':'Execution needs attention')}</h3><p>${esc(guidance)}</p></div><span class="status-pill ${cls}">${esc(blocked?'BLOCKED':goal)}</span></div>
    ${lifecycleStrip({blocked,executionStatus,goal})}
    <div class="monitor-summary operator-summary">
      ${monitorMetric('Execution result',executionStatus,'Did Tool 13 apply the simulation command?',statusClass(executionStatus))}
      ${monitorMetric('Goal result',goal,'Post-execution operational verification',goal==='ACHIEVED'?'good':goal==='NOT_ACHIEVED'?'bad':'warn')}
      ${monitorMetric('Command tracking',commandMatch===true?(hasNormalVariance?'Within tolerance':'Exact'):commandMatch===false?'Outside tolerance':'Not available','Requested actions compared with actual actions using the configured execution tolerance',commandMatch===true?'good':commandMatch===false?'bad':'neutral')}
      ${monitorMetric('Replan required',replan?'YES':'NO','Based on execution and verified operational result',replan?'bad':'good')}
    </div>
    ${checks?`<div class="operator-section"><div class="operator-section-head"><div><span class="eyebrow">SAFETY CHECKS</span><h4>Pre-execution checks</h4></div></div><div class="safety-checks">${checks}</div></div>`:''}
    ${!blocked?`<div class="operator-section"><div class="operator-section-head"><div><span class="eyebrow">BEFORE VS AFTER</span><h4>What changed after execution</h4></div></div>${beforeAfterTable(diagnosis.before_after||{})}</div>
    <div class="operator-section"><div class="operator-section-head"><div><span class="eyebrow">EXPECTED VS ACTUAL</span><h4>Requested dispatch compared with actual execution</h4></div></div>${expectedActualTable(diagnosis.expected_vs_actual||[])}</div>
    <div class="operator-section"><div class="operator-section-head"><div><span class="eyebrow">OPERATOR SUMMARY</span><h4>Evidence / reasons</h4></div></div><div class="evidence-cards">
      ${monitorMetric('Grid balance',passText(diagnosis.balance_ok),'Residual imbalance against configured tolerance',diagnosis.balance_ok===true?'good':diagnosis.balance_ok===false?'bad':'neutral')}
      ${monitorMetric('Reserve',passText(diagnosis.reserve_ok),diagnosis.reserve_actual_mw!=null&&diagnosis.reserve_required_mw!=null?`Actual ${fmt(diagnosis.reserve_actual_mw,2)} MW · Required ${fmt(diagnosis.reserve_required_mw,2)} MW`:'Verified reserve evidence',diagnosis.reserve_ok===true?'good':diagnosis.reserve_ok===false?'bad':'neutral')}
      ${monitorMetric('Goal verification',goal,diagnosis.verification_status||'Post-execution check',goal==='ACHIEVED'?'good':goal==='NOT_ACHIEVED'?'bad':'warn')}
      ${monitorMetric('Reusable lesson',lesson.status==='CREATED_OR_REINFORCED'?`Lesson #${lesson.lesson_id}`:'History only',lesson.reason||'Memory evaluated after operational outcome',lesson.status==='CREATED_OR_REINFORCED'?'good':'neutral')}
    </div></div>
    ${timeline.length?`<div class="operator-section"><div class="operator-section-head"><div><span class="eyebrow">EVENT TIMELINE</span><h4>Execution and verification</h4></div></div><div class="event-timeline">${timeline.map(x=>`<div><time>${esc(new Date(x[0]).toLocaleString())}</time><span>${esc(x[1])}</span></div>`).join('')}</div></div>`:''}`:''}
    <div class="operator-guidance ${cls}"><i class="fa-solid fa-user-shield"></i><div><b>Operator guidance</b><p>${esc(guidance)}</p></div></div>
    ${technicalDetails(result)}`;
  const automaticReplan=result?.post_execution?.replan||{};
  if(!blocked&&['REPLAN_GENERATED','REPLAN_ALREADY_EXISTS'].includes(String(automaticReplan.status||'').toUpperCase())){
    box.insertAdjacentHTML('beforeend',replacementRecoverySection(automaticReplan.plans||[],'AUTOMATIC POST-EXECUTION RECOVERY'));
    wireRecoveryApprovalActions(box);
  }
  if(!blocked&&replan){
    const planId=execution.plan_id??result?.plan_id;
    if(planId!=null){
      const action=document.createElement('div');
      action.className='operator-action-bar';
      action.innerHTML=`<button class="primary compact" type="button" id="postExecutionReplanButton"><i class="fa-solid fa-arrows-rotate"></i> REPLAN</button>`;
      box.append(action);
      $('#postExecutionReplanButton').onclick=async()=>{
        const b=$('#postExecutionReplanButton');
        b.disabled=true;b.textContent='Replanning…';
        try{
          await replanPlan(Number(planId));
          toast(`Replacement planning completed for Plan #${planId}`);
        }catch(e){
          toast(e?.message||'Replanning failed');
        }finally{
          if(b.isConnected){b.disabled=false;b.innerHTML='<i class="fa-solid fa-arrows-rotate"></i> REPLAN';}
        }
      };
    }
  }
}
async function replanPlan(planId){
  const d=await post(`/api/plans/${planId}/replan`,{required_candidate_count:4},{timeoutMs:180000});
  const r=d.result||{};
  const plans=Array.isArray(r.plans)?r.plans:[];
  const box=$('#monitorResult');
  const reused=r.status==='REPLAN_ALREADY_EXISTS';
  box.className='monitor-dashboard';
  box.innerHTML=`
    <div class="operator-hero ${plans.length?'warn':'bad'}">
      <div class="operator-hero-icon"><i class="fa-solid fa-arrows-rotate"></i></div>
      <div><span class="eyebrow">POST-EXECUTION RECOVERY</span><h3>${esc(reused?'Existing replacement reused':plans.length?'Replacement plans generated':'Replacement planning incomplete')}</h3><p>${esc(reused?'An active replacement already exists, so no duplicate candidates were created.':'The replacement was generated from the latest operational snapshot and remains subject to the normal approval and safety workflow.')}</p></div>
      <span class="status-pill ${plans.length?'warn':'bad'}">${esc(r.status||'UNKNOWN')}</span>
    </div>
    <div class="monitor-summary operator-summary">
      ${monitorMetric('Original plan',`#${r.original_plan_id??planId}`,'Failed/partial execution that triggered recovery','neutral')}
      ${monitorMetric('Planning snapshot',`#${r.current_snapshot_id??'—'}`,'Latest valid operational snapshot used for replanning','good')}
      ${monitorMetric('Parent lineage',r.parent_plan_id==null?'Not available':`Plan #${r.parent_plan_id}`,'Replacement candidates preserve the original plan lineage',r.parent_plan_id!=null?'good':'bad')}
      ${monitorMetric('Auto approval','NO','Human Approval remains mandatory','good')}
    </div>
    ${replacementRecoverySection(plans,'REPLACEMENT CANDIDATES')}
    <div class="operator-guidance warn"><i class="fa-solid fa-user-shield"></i><div><b>Operator control remains active</b><p>Review a feasible replacement, approve it normally, allow SafetyAgent to validate it, then execute through the existing lifecycle.</p></div></div>
    ${technicalDetails(d)}`;
  wireRecoveryApprovalActions(box);
  await loadPlans();
}
function renderPlanDetails(title,p){
  const box=$('#monitorResult');
  box.className='monitor-dashboard';
  box.innerHTML=`
    <div class="operator-hero ${statusClass(p.status)}"><div class="operator-hero-icon"><i class="fa-solid fa-file-circle-check"></i></div><div><span class="eyebrow">PLAN SUMMARY</span><h3>${esc(planLabel(p))}</h3><p>Operator summary for the selected plan.</p></div><span class="status-pill ${statusClass(p.status)}">${esc(p.status||'UNKNOWN')}</span></div>
    <div class="monitor-summary operator-summary">
      ${monitorMetric('Plan ID',`#${p.id??'—'}`)}
      ${monitorMetric('Strategy',String(p.strategy_label||'Operational').replaceAll('_',' '))}
      ${monitorMetric('Baseline snapshot',`#${p.snapshot_id??'—'}`)}
      ${monitorMetric('Status',p.status||'UNKNOWN','Human approval and lifecycle status',statusClass(p.status))}
    </div>
    ${p.dynamic_pricing?`<div class="operator-section"><div class="operator-section-head"><div><span class="eyebrow">DYNAMIC PRICING</span><h4>Tool 17 planning context</h4></div><span class="operator-event">Tool #${esc(p.dynamic_pricing.tool_call_id)}</span></div><div class="monitor-summary operator-summary">${monitorMetric('Flexible shift',p.dynamic_pricing.total_shifted_mw==null?'—':`${fmt(p.dynamic_pricing.total_shifted_mw,2)} MW`)}${monitorMetric('Minimum price',p.dynamic_pricing.minimum_price_per_mwh==null?'—':`$${fmt(p.dynamic_pricing.minimum_price_per_mwh,2)}/MWh`)}${monitorMetric('Expected surplus reduction',p.dynamic_pricing.expected_impact?.expected_surplus_reduction_mw==null?'—':`${fmt(p.dynamic_pricing.expected_impact.expected_surplus_reduction_mw,2)} MW`)}${monitorMetric('Energy scope',p.dynamic_pricing.energy_scope||'SOLAR_ONLY')}</div></div>`:''}
    ${technicalDetails(p)}`;
}
function renderMonitoring(title,result){
  if(result?.monitoring)return renderMonitorCheck(title,result);
  if(result?.impact)return renderRevalidation(title,result);
  if(result?.safety||result?.execution||result?.status==='BLOCKED_BY_SAFETY'||result?.status==='EXECUTION_ATTEMPTED')return renderExecution(title,result);
  if(result&&typeof result==='object'&&result.id&&result.snapshot_id)return renderPlanDetails(title,result);
  const box=$('#monitorResult');
  box.className='monitor-dashboard';
  box.innerHTML=`<div class="operator-hero neutral"><div class="operator-hero-icon"><i class="fa-solid fa-circle-info"></i></div><div><span class="eyebrow">OPERATION RESULT</span><h3>${esc(title)}</h3><p>The result is available below.</p></div></div>${technicalDetails(result)}`;
}

function renderDynamicPricing(result, title='Dynamic Pricing analysis'){
  latestDynamicPricing=result||null;
  const box=$('#dynamicPricingResult');
  if(!box)return;
  const d=toolData(result||{});
  if(!result){
    box.className='dynamic-pricing-result empty-state-card';
    box.innerHTML='<i class="fa-solid fa-tags"></i><b>No Dynamic Pricing analysis yet</b><span>Analyze the current snapshot to see whether solar surplus can be absorbed by flexible demand.</span>';
    return;
  }
  const impact=d.expected_impact||{};
  const hourly=Array.isArray(d.hourly_analysis)?d.hourly_analysis:[];
  const priced=hourly.filter(x=>x.final_price_per_mwh!=null);
  const minPrice=priced.length?Math.min(...priced.map(x=>Number(x.final_price_per_mwh))):null;
  const maxDiscount=priced.length?Math.max(...priced.map(x=>Number(x.discount_ratio||0))):null;
  const severityOrder={UNKNOWN:-1,NONE:0,LOW:1,MEDIUM:2,HIGH:3,CRITICAL:4};
  const maxSeverity=hourly.reduce((best,row)=>severityOrder[String(row.severity||'UNKNOWN').toUpperCase()]>severityOrder[best]?String(row.severity).toUpperCase():best,'UNKNOWN');
  const action=d.action_required===true;
  const status=d.domain_status||'UNKNOWN';
  const reason=(d.reasons||[])[0]||(action?'A conserved flexible-load shift is available.':'No pricing action is required for the current horizon.');
  box.className='dynamic-pricing-result';
  box.innerHTML=`
    <div class="operator-hero ${action?'good':statusClass(status)}"><div class="operator-hero-icon"><i class="fa-solid fa-tags"></i></div><div><span class="eyebrow">TOOL 17 · SOLAR ONLY</span><h3>${esc(title)}</h3><p>${esc(reason)}</p></div><span class="status-pill ${action?'good':statusClass(status)}">${action?'ACTION AVAILABLE':esc(String(status).replaceAll('_',' '))}</span></div>
    <div class="monitor-summary operator-summary">
      ${monitorMetric('Solar surplus severity',maxSeverity,'Highest severity in the current planning horizon',statusClass(maxSeverity))}
      ${monitorMetric('Flexible load shift',d.total_shifted_mw==null?'Not available':`${fmt(d.total_shifted_mw,2)} MW`,'Conserved source-to-target load movement',action?'good':'neutral')}
      ${monitorMetric('Lowest dynamic price',minPrice==null?'Not available':`$${fmt(minPrice,2)}/MWh`,maxDiscount==null?'No price signal':`Maximum discount ${fmt(maxDiscount*100,1)}%`,action?'good':'neutral')}
      ${monitorMetric('Expected surplus reduction',impact.expected_surplus_reduction_mw==null?'Not available':`${fmt(impact.expected_surplus_reduction_mw,2)} MW`,'Deterministic Tool 17 expected impact',impact.expected_surplus_reduction_mw>0?'good':'neutral')}
    </div>
    ${(d.warnings||[]).length?`<div class="operator-guidance warn"><i class="fa-solid fa-triangle-exclamation"></i><div><b>Analysis notes</b><p>${esc((d.warnings||[]).join(' · '))}</p></div></div>`:''}
    ${technicalDetails(result)}`;
}

function planDynamicPricingBadge(p){
  const dp=p?.dynamic_pricing;
  if(!dp)return '';
  const shift=dp.total_shifted_mw==null?'':` · ${fmt(dp.total_shifted_mw,1)} MW shifted`;
  return `<span class="badge good"><i class="fa-solid fa-tags"></i> Dynamic Pricing${esc(shift)}</span>`;
}

function renderRecentPlans(plans=[]){const box=$('#recentPlans');box.innerHTML='';if(!plans.length){box.innerHTML='<div class="empty">No plans available.</div>';return}plans.slice(0,4).forEach(p=>{const el=document.createElement('div');el.className='recent-plan';el.innerHTML=`<div><b>${esc(planLabel(p))}</b><small>${esc((p.strategy_label||'Operational').replaceAll('_',' '))}</small></div><span class="badge ${statusClass(p.status)}">${esc(p.status||'UNKNOWN')}</span>`;box.append(el)})}
async function loadPlans(){
  try{
    const d=await api('/api/plans');
    renderRecentPlans(d.plans||[]);
    const box=$('#plansList');box.innerHTML='';
    if(!d.plans?.length){
      box.innerHTML='<div class="panel empty">No plans are available for the current snapshot. Generate a fresh set of plans.</div>';
      return;
    }
    const feasibleCount=d.plans.filter(p=>String(p.validation_status||'').toUpperCase()==='FEASIBLE').length;
    const infeasibleCount=d.plans.filter(p=>String(p.validation_status||'').toUpperCase()==='INFEASIBLE').length;
    const unknownCount=d.plans.length-feasibleCount-infeasibleCount;
    const validationRuntimeErrors=d.plans.filter(p=>String(p.validation_reason||'').startsWith('Validation pipeline error')).length;
    const summary=document.createElement('div');
    if(feasibleCount>0){
      summary.className='panel operator-note good';
      summary.innerHTML=`<b>${feasibleCount} executable candidate${feasibleCount===1?'':'s'} available</b><p>Only plans that passed deterministic validation for Snapshot #${esc(d.current_snapshot_id??'—')} can be approved.</p>`;
    }else if(infeasibleCount===d.plans.length){
      summary.className='panel operator-note bad';
      summary.innerHTML=`<b>No executable plan satisfies the approved engineering constraints</b><p>The candidates remain visible for diagnosis. Review each candidate reason below; approval stays disabled until deterministic validation passes.</p>`;
    }else{
      summary.className='panel operator-note warn';
      if(validationRuntimeErrors>0){
        summary.innerHTML=`<b>Engineering validation did not finish</b><p>${validationRuntimeErrors} candidate${validationRuntimeErrors===1?'':'s'} encountered a validation runtime issue. Use Retry Validation; approval remains safely disabled until Tool 09 completes.</p>`;
      }else{
        summary.innerHTML=`<b>Plan validation evidence is incomplete</b><p>${unknownCount} candidate${unknownCount===1?'':'s'} require refreshed deterministic evidence before approval.</p>`;
      }
    }
    box.append(summary);
    d.plans.forEach(p=>{
      const validation=String(p.validation_status||'NOT_VALIDATED').toUpperCase();
      const el=document.createElement('article');el.className='panel plan-row';
      const validationHint=p.validation_operator_message||(
        validation==='FEASIBLE'?'Engineering checks passed for this planning snapshot.':
        validation==='INFEASIBLE'?'No safe dispatch satisfies all approved constraints for this candidate.':
        'Engineering validation is incomplete.'
      );
      el.innerHTML=`<div class="plan-main"><div class="plan-title-row"><b>${esc(planLabel(p))}</b><span class="badge ${statusClass(p.status)}">${esc(p.status)}</span><span class="badge ${statusClass(validation)}">${esc(validation.replaceAll('_',' '))}</span>${planDynamicPricingBadge(p)}</div><p>Plan #${p.id} · ${esc((p.strategy_label||'Operational').replaceAll('_',' '))} · Snapshot #${p.snapshot_id??'—'}${p.parent_plan_id!=null?` · Parent Plan #${esc(p.parent_plan_id)}`:''}${p.trigger_context?.trigger_type?` · Trigger: ${esc(String(p.trigger_context.trigger_type).replaceAll('_',' '))}`:''}</p><small>${esc(validationHint)}</small>${p.dynamic_pricing?`<small class="dp-plan-note">Tool 17 #${esc(p.dynamic_pricing.tool_call_id)} · Expected surplus reduction ${p.dynamic_pricing.expected_impact?.expected_surplus_reduction_mw==null?'—':`${fmt(p.dynamic_pricing.expected_impact.expected_surplus_reduction_mw,1)} MW`}</small>`:''}${p.data_source_details?.length?`<small>External provenance: ${esc(p.data_source_details.map(x=>`${x.source} ${x.status}`).join(' · '))}</small>`:''}</div><div class="actions"></div>`;
      const a=el.querySelector('.actions');
      if(p.status==='PROPOSED'&&p.can_approve){
        a.append(button('Approve',()=>planAction(p.id,'approve'),'primary compact'),button('Reject',()=>planAction(p.id,'reject'),'secondary compact'));
      }else if(p.status==='PROPOSED'){
        if(validation==='INFEASIBLE'){
          const b=button('Not Executable',()=>{},'secondary compact');b.disabled=true;a.append(b);
        }else{
          a.append(button('Retry Validation',()=>validateCandidate(p.id),'secondary compact'));
        }
        a.append(button('Reject',()=>planAction(p.id,'reject'),'secondary compact'));
      }
      if(p.status==='APPROVED')a.append(
        button('Check Changes',()=>monitorPlan(p.id,'monitor'),'secondary compact'),
        button('Recheck Plan',()=>monitorPlan(p.id,'revalidate'),'secondary compact'),
        button('Execute Plan',()=>planAction(p.id,'execute'),'primary compact')
      );
      a.append(button('View Details',async()=>{const x=await api(`/api/plans/${p.id}`);renderMonitoring(`Plan #${p.id}`,x.plan);view('monitoring')},'secondary compact'));
      box.append(el);
    });
    if(d.archive_count>0){
      const note=document.createElement('div');note.className='panel empty';
      note.textContent=`${d.archive_count} older plan(s) are archived because they belong to previous grid snapshots.`;
      box.append(note);
    }
  }catch(e){toast(e.message)}
}
function button(t,fn,c){
  const b=document.createElement('button');
  b.className=c;b.textContent=t;
  b.onclick=async()=>{
    const original=b.textContent;
    try{
      b.disabled=true;
      await fn();
    }catch(e){
      toast(e?.message||'Operation failed');
    }finally{
      if(b.isConnected){b.disabled=false;b.textContent=original;}
    }
  };
  return b;
}
async function validateCandidate(id){
  try{
    if(!latest?.id)throw new Error('No current grid snapshot is available. Run a state assessment first.');
    const d=await post(`/api/plans/${id}/validate`,{current_snapshot_id:latest.id});
    const bundle=d.result?.evaluation||{};
    const evaluations=Array.isArray(bundle.evaluations)?bundle.evaluations:[];
    const ev=evaluations.find(x=>Number(x.plan_id)===Number(id))||evaluations[0]||bundle;
    const state=ev.is_feasible===true?'FEASIBLE':ev.is_feasible===false?'INFEASIBLE':'UNKNOWN';
    toast(`Plan #${id} validation: ${state}`);
    await loadPlans();
    await loadSummary();
  }catch(e){
    toast(`Validation retry failed: ${e.message}`);
  }
}
async function planAction(id,a){
  try{
    let body={};
    if(a==='execute'){
      if(!latest?.id)throw new Error('No current grid snapshot is available. Run a state assessment first.');
      body={current_snapshot_id:latest.id};
    }
    if(a==='execute')toast('Execution started immediately. Result will be revealed after 10 seconds.',9500);
    // Start the backend execution NOW. The 10-second rule is only a UI reveal
    // gate: it never delays sending or processing the Execute request.
    const request=a==='execute'
      ? api(`/api/plans/${id}/${a}`,{method:'POST',body:JSON.stringify(body),timeoutMs:180000})
      : post(`/api/plans/${id}/${a}`,body);
    const d=a==='execute'?await settleAfterMinimum(request,10000):await request;
    if(a==='execute'){
      renderMonitoring(`Execution · Plan #${id}`,d.result);
      view('monitoring');
      const blocked=d.result?.status==='BLOCKED_BY_SAFETY';
      const mem=d.result?.post_execution?.memory||{};const memSaved=mem.status==='RECORDED'||mem.historical_case_available===true;toast(blocked?'Execution stopped by the safety gate':(memSaved?'Execution completed and saved to operational memory':d.result?.status==='VERIFICATION_PENDING'?'Execution completed; outcome verification is pending':'Execution request completed'));
    }else{
      toast(`Plan ${a} completed`);
    }
    await loadPlans();
    await loadSummary();
  }catch(e){toast(e.message)}
}
async function monitorPlan(id,a){
  try{
    if(!latest?.id)throw new Error('No current grid snapshot is available. Run a state assessment first.');
    const d=await post(`/api/plans/${id}/${a}`,{current_snapshot_id:latest.id},{timeoutMs:90000});
    renderMonitoring(`${a==='monitor'?'Change Check':'Plan Recheck'} · Plan #${id}`,d.result);
    view('monitoring');
    if(a==='revalidate')await loadPlans();
  }catch(e){toast(e.message)}
}
async function currentReserveAssessmentId(){
  const assessedReserve=toolData(latestAssessment?.reserve);
  const assessmentSnapshotId=Number(latestAssessment?.snapshot_id??toolData(latestAssessment?.state)?.snapshot_id);
  return (assessmentSnapshotId===Number(latest?.id))?assessedReserve.assessment_id:null;
}

$('#analyzeDynamicPricing').onclick=async()=>{
  const b=$('#analyzeDynamicPricing');const original=b.textContent;
  try{
    b.disabled=true;b.textContent='Analyzing…';
    if(!latest?.id)throw new Error('Run or wait for a state snapshot first');
    const d=await post('/api/dynamic-pricing/analyze',{snapshot_id:latest.id,horizon_minutes:DEFAULT_HORIZON_MINUTES},{timeoutMs:120000});
    renderDynamicPricing(d.result);
    toast(d.result?.action_required===true?'Dynamic Pricing action is available':'Dynamic Pricing analysis completed; no load shift is required');
  }catch(e){toast(e.message)}finally{b.disabled=false;b.textContent=original}
};

$('#generateDynamicPricingPlan').onclick=async()=>{
  const b=$('#generateDynamicPricingPlan');const original=b.textContent;
  try{
    b.disabled=true;b.textContent='Building DP Plans…';
    if(!latest?.id)throw new Error('Run or wait for a state snapshot first');
    const reserveId=await currentReserveAssessmentId();
    const body={snapshot_id:latest.id,horizon_minutes:DEFAULT_HORIZON_MINUTES,required_candidate_count:4,reserve_assessment_id:reserveId||null};
    const d=await post('/api/dynamic-pricing/plans',body,{timeoutMs:180000});
    if(d.result?.dynamic_pricing)renderDynamicPricing(d.result.dynamic_pricing,'Dynamic Pricing planning analysis');
    const status=String(d.result?.status||'UNKNOWN').toUpperCase();
    if(status==='NO_ACTION')toast('No Dynamic Pricing action is required for this horizon');
    else if(status==='PLANS_EVALUATED')toast(d.result?.selected_plan_id?`Dynamic Pricing plans evaluated; selected candidate #${d.result.selected_plan_id}`:'Dynamic Pricing plans evaluated; review the candidates');
    else toast('Dynamic Pricing analysis is incomplete; no plan was created');
    await loadPlans();
  }catch(e){toast(e.message)}finally{b.disabled=false;b.textContent=original}
};

$('#generatePlan').onclick=async()=>{
  const b=$('#generatePlan');const original=b.textContent;
  try{
    b.disabled=true;b.textContent='Generating…';
    if(!latest?.id)throw new Error('Run or wait for a state snapshot first');
    const reserveId=await currentReserveAssessmentId();
    const d=await post('/api/plans/generate',{snapshot_id:latest.id,horizon_minutes:DEFAULT_HORIZON_MINUTES,required_candidate_count:4,reserve_assessment_id:reserveId||null},{timeoutMs:120000});
    const selected=d.result?.selected_plan_id;
    toast(selected?`Plans generated; selected candidate #${selected}`:'Plans generated; review deterministic validation results');
    await loadPlans();
  }catch(e){toast(e.message)}finally{b.disabled=false;b.textContent=original}
};

async function loadRuns(){
  try{
    const d=await api('/api/agent/runs?limit=30'),box=$('#runsList');box.innerHTML='';
    if(!d.runs?.length){box.innerHTML='<div class="panel empty">No agent runs.</div>';return}
    d.runs.forEach(r=>{
      const ctx=r.trigger_context||{};
      const el=document.createElement('article');el.className='panel run-row';
      const trigger=ctx.trigger_type?String(ctx.trigger_type).replaceAll('_',' '):runLabel(r.trigger_source);
      el.innerHTML=`<div><b>Run #${r.id} · ${esc(trigger)}</b><span class="badge ${statusClass(r.status)}">${esc(r.status)}</span><p>${r.start_time?new Date(r.start_time).toLocaleString():'—'} · ${esc(r.summary||'Auditable backend operation')}${ctx.suppressed_by_cooldown?' · Duplicate event suppressed':''}</p></div>`;
      el.append(button('View Tool Steps',async()=>{
        const x=await api(`/api/agent/runs/${r.id}`),calls=x.tool_calls||[],run=x.run||{},context=run.trigger_context||{},evidence=x.engineering_evidence||[],external=x.external_data||[];
        const cards=calls.length?calls.map(c=>{
          const m=toolMeta(c.tool_name);
          return `<div class="tool-trace-card"><div class="tool-trace-no">${esc(m.number)}</div><div class="tool-trace-main"><div class="tool-trace-head"><b>Tool ${esc(m.number)} — ${esc(m.name)}</b><span class="badge ${statusClass(c.status)}">${esc(c.status||'UNKNOWN')}</span></div><p>${esc(m.purpose)}</p><small>${esc(c.tool_category||'Backend')} · ${c.latency_ms==null?'Duration —':`${esc(c.latency_ms)} ms`}</small><details class="technical-details"><summary>Technical input / output</summary><pre>${esc(JSON.stringify({input:c.input_json,output:c.output_json,error:c.error_message},null,2))}</pre></details></div></div>`;
        }).join(''):'<div class="panel empty">No tool steps were recorded for this run.</div>';
        const evidenceHtml=evidence.length?`<div class="engineering-evidence"><h4>Engineering Evidence Used</h4>${evidence.map(ev=>`<div class="evidence-card"><span>${esc(ev.consumer||'Engineering evidence')}</span><strong>${esc(ev.document||'Document')}</strong><p>${ev.section?`${esc(String(ev.section))} · `:''}${esc(ev.relevance_summary||'Relevant retrieved evidence')} · ${ev.retrieval_timestamp?new Date(ev.retrieval_timestamp).toLocaleString():'time unavailable'}</p></div>`).join('')}</div>`:'<div class="engineering-evidence"><h4>Engineering Evidence Used</h4><div class="evidence-card"><span>Tool 16</span><strong>No RAG evidence stored for this run</strong><p>Deterministic validation remains authoritative whether or not supporting evidence was retrieved.</p></div></div>';
        const externalHtml=external.length?`<div class="source-context-strip">${external.map(src=>`<span class="badge ${statusClass(src.status)}">${esc(src.source)} · ${esc(String(src.status).replaceAll('_',' '))}</span>`).join('')}</div>`:'';
        el.className='panel run-inspect';
        el.innerHTML=`<div class="run-inspect-head"><div><span class="eyebrow">RUN #${esc(r.id)}</span><h3>${esc(context.trigger_type?String(context.trigger_type).replaceAll('_',' '):runLabel(run.trigger_source))}</h3><p>${esc(run.summary||'Recorded agent/tool execution trace.')}</p></div><span class="badge ${statusClass(run.status)}">${esc(run.status||'UNKNOWN')}</span></div><div class="run-context"><div><span>Trigger source</span><strong>${esc(context.trigger_source||run.trigger_source||'—')}</strong></div><div><span>Snapshot / Event</span><strong>${context.snapshot_id?`Snapshot #${esc(context.snapshot_id)}`:'—'}${context.event_id?` · Event #${esc(context.event_id)}`:''}</strong></div><div><span>Control gate</span><strong>${run.human_approval_required===true?'Human Approval required':'Standard lifecycle'}</strong></div></div>${context.reason?`<div class="operator-note"><b>Why this run started</b><p>${esc(context.reason)}</p></div>`:''}${externalHtml}${evidenceHtml}<div class="tool-trace-list">${cards}</div>`;
      },'secondary compact'));
      box.append(el)
    })
  }catch(e){toast(e.message)}
}

function triState(v,yes='PASS',no='FAIL'){return v===true?yes:v===false?no:'Not available'}
async function loadMemory(){
  try{
    const [historyData,lessonData]=await Promise.all([api('/api/memory/history?limit=30'),api('/api/memory/lessons?limit=50')]);
    const historyBox=$('#historyList'),lessonBox=$('#lessonsList');historyBox.innerHTML='';lessonBox.innerHTML='';
    if(!historyData.history?.length){historyBox.innerHTML='<div class="panel empty">No execution attempts have been recorded yet.</div>'}
    else historyData.history.forEach(item=>{
      const e=item.execution||{},p=item.plan||{},o=item.outcome||{},sum=item.operator_summary||{};
      const outcomeState=sum.goal_result|| (item.outcome?(o.is_success===true?'ACHIEVED':o.is_success===false?'NOT ACHIEVED':'NOT EVALUATED'):(e.id?'PENDING':'NOT EXECUTED'));
      const when=e.execution_timestamp;
      const el=document.createElement('article');el.className='panel memory-execution-card';
      el.innerHTML=`<div class="memory-execution-head"><div><b>Execution #${esc(e.id||'—')} · ${esc(p.plan_name||`Plan #${e.plan_id||'—'}`)}</b><p>${esc(p.strategy_label||'Operational plan')} · ${when?new Date(when).toLocaleString():'—'}</p></div><span class="badge ${statusClass(e.status)}">${esc(e.status||'UNKNOWN')}</span></div>
      <div class="memory-execution-grid"><div><span>Plan</span><strong>#${esc(e.plan_id||'—')}</strong></div><div><span>Command Execution Quality</span><strong class="${statusClass(sum.execution_result)}">${esc(sum.execution_result||e.status||'—')}</strong></div><div><span>Operational Goal</span><strong class="${statusClass(outcomeState)}">${esc(String(outcomeState).replaceAll('_',' '))}</strong></div><div><span>Replanning</span><strong class="${sum.replan_required?'bad':'good'}">${sum.replan_required?'Required':'Not required'}</strong></div></div>
      ${item.outcome?`<div class="semantic-help">Command Execution Quality measures requested-vs-actual command tracking. Operational Goal measures whether the post-execution grid objective was achieved.</div><div class="operator-section memory-evidence"><div class="operator-section-head"><div><span class="eyebrow">OPERATOR SUMMARY</span><h4>Evidence / reasons</h4></div></div><div class="evidence-cards">${monitorMetric('Execution result',sum.execution_result||e.status||'—','Command execution record',statusClass(sum.execution_result||e.status))}${monitorMetric('Grid balance',triState(sum.grid_balance),'Configured residual-imbalance criterion',sum.grid_balance===true?'good':sum.grid_balance===false?'bad':'neutral')}${monitorMetric('Reserve',triState(sum.reserve),sum.reserve_actual_mw!=null&&sum.reserve_required_mw!=null?`Actual ${fmt(sum.reserve_actual_mw,2)} MW · Required ${fmt(sum.reserve_required_mw,2)} MW`:'Configured reserve criterion',sum.reserve===true?'good':sum.reserve===false?'bad':'neutral')}${monitorMetric('Replanning',sum.replan_required?'Required':'Not required','Based on final operational result',sum.replan_required?'bad':'good')}${sum.dynamic_pricing?monitorMetric('Dynamic Pricing',String(sum.dynamic_pricing.goal_result||'NOT_EVALUATED').replaceAll('_',' '),sum.dynamic_pricing.actual_load_shift_mw==null?'Impact evidence incomplete':`Actual flexible shift ${fmt(sum.dynamic_pricing.actual_load_shift_mw,2)} MW`,statusClass(sum.dynamic_pricing.goal_result)):''}</div></div><details class="technical-details"><summary><i class="fa-solid fa-code"></i> Technical details (engineering / IT)</summary><pre>${esc(JSON.stringify({execution:e,outcome:o},null,2))}</pre></details>`:''}`;
      historyBox.append(el)
    });
    if(!lessonData.lessons?.length){lessonBox.innerHTML='<div class="panel empty">No reusable lessons stored yet. A single successful execution stays in history; reusable success patterns need repeated evidence.</div>'}
    else lessonData.lessons.forEach(l=>{const el=document.createElement('article');el.className='panel lesson-card';const confirmed=l.last_confirmed_at?new Date(l.last_confirmed_at).toLocaleString():'Not available';el.innerHTML=`<div class="lesson-head"><b>Lesson #${l.id}</b><span class="badge">Confidence ${l.confidence_percent==null?'—':`${l.confidence_percent}%`}</span></div><div class="lesson-readable"><span class="eyebrow">PATTERN</span><h4>${esc(l.observed_pattern)}</h4><div class="lesson-fields"><div><span>Observed result</span><strong>${esc(l.operational_impact||'Evidence-backed operational pattern')}</strong></div><div><span>Evidence</span><strong>${esc(l.evidence_summary)}</strong></div><div><span>Frequency</span><strong>${esc(`${l.frequency_count||1} confirmed occurrence${Number(l.frequency_count||1)===1?'':'s'}`)}</strong></div><div><span>Last confirmed</span><strong>${esc(confirmed)}</strong></div><div class="wide"><span>Operational use</span><strong>${esc(l.planning_implication)}</strong></div><div><span>Source</span><strong>${l.source_execution_id?`Execution #${esc(l.source_execution_id)}`:'Historical evidence'}</strong></div></div></div><div class="eligibility-note"><b>Lesson Eligibility Reason:</b> ${esc(l.eligibility_reason||'Evidence and repeatability rules qualified this lesson.')}</div><details class="technical-details"><summary><i class="fa-solid fa-code"></i> Technical details</summary><pre>${esc(JSON.stringify(l,null,2))}</pre></details>`;lessonBox.append(el)});
  }catch(e){toast(e.message)}
}
async function loadLessons(){return loadMemory()}


$('#refreshExternalData').onclick=async()=>{try{const d=await post('/api/external-data/refresh',{source:'ALL'},{timeoutMs:90000});renderExternalSources(d.sources||[]);toast('External sources refreshed')}catch(e){toast(e.message)}};
$('#reloadRuns').onclick=loadRuns;$('#reloadLessons').onclick=loadMemory;$('#refreshBtn').onclick=async()=>{await Promise.allSettled([loadSummary(),loadPlans(),loadMonitorStatus(),loadMiniMap()]);toast('Dashboard refreshed')};

await Promise.allSettled([health(),loadSummary(),loadPlans(),loadMonitorStatus(),loadMiniMap()]);
setInterval(()=>Promise.allSettled([loadSummary(),loadMonitorStatus()]),30000);
