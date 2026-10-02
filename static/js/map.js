import {api} from './api.js';

let map;
let layer;
let miniMap;
let miniLayer;
let timer;

const statusColor=status=>({
  GREEN:'#16945b',
  YELLOW:'#d79218',
  RED:'#cf3f45'
}[String(status||'YELLOW').toUpperCase()]||'#d79218');

const statusLabel=status=>({
  GREEN:'Healthy',
  YELLOW:'Needs attention',
  RED:'Critical'
}[String(status||'YELLOW').toUpperCase()]||'Needs attention');

const esc=value=>String(value??'—').replace(/[&<>'"]/g,char=>({
  '&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'
}[char]));

function technologySvg(){
  return `<svg viewBox="0 0 40 40" aria-hidden="true">
    <circle cx="29" cy="10" r="4.4" class="plant-svg-fill"/>
    <path d="M29 2.5v3M29 14.5v3M21.5 10h3M33.5 10h3M23.7 4.7l2.1 2.1M32.2 13.2l2.1 2.1M34.3 4.7l-2.1 2.1" class="plant-svg-line"/>
    <path d="M7 17.2h23.8l3.2 14H4z" class="plant-svg-line"/>
    <path d="M10.2 21.7h20.1M8.3 26.4h23.1M15.5 17.4l-1.8 14M23.2 17.4l1.7 14" class="plant-svg-line thin"/>
    <path d="M18.7 31.6v4.2M13.2 35.8h11" class="plant-svg-line"/>
  </svg>`;
}

function markerIcon(p,small=false){
  const tech='SOLAR';
  const techClass='solar';
  const techCode='S';
  const width=small?34:44;
  const height=small?40:52;
  const html=`<div class="plant-marker ${small?'plant-marker-small':''} tech-${techClass}" style="--marker:${statusColor(p.status)}">
    <div class="plant-marker-halo"></div>
    <div class="plant-marker-core">${technologySvg()}</div>
    <span class="plant-tech-badge">${techCode}</span>
    <span class="plant-marker-tip"></span>
  </div>`;
  return L.divIcon({
    className:'solargrid-plant-divicon',
    html,
    iconSize:[width,height],
    iconAnchor:[width/2,height-3],
    popupAnchor:[0,-height+8]
  });
}

function popup(plant){
  const tech='SOLAR';
  const output=plant.current_output_mw==null?null:Number(plant.current_output_mw);
  const capacity=plant.capacity_mw==null?null:Number(plant.capacity_mw);
  const utilization=output!=null&&capacity>0?Math.max(0,Math.min(100,(output/capacity)*100)):null;
  const techName='Solar plant';
  const techIcon='fa-sun';
  const location=[plant.location,plant.governorate].filter(Boolean).join(', ');
  return `<div class="plant-popup">
    <div class="plant-popup-head">
      <div class="plant-popup-tech ${tech.toLowerCase()}"><i class="fa-solid ${techIcon}"></i></div>
      <div><span>${esc(techName)}</span><h4>${esc(plant.name)}</h4><p><i class="fa-solid fa-location-dot"></i> ${esc(location||'Location unavailable')}</p></div>
    </div>
    <div class="plant-popup-status"><span class="popup-status-dot" style="--status:${statusColor(plant.status)}"></span><b>${esc(`Demo status: ${statusLabel(plant.status)}`)}</b><small>${esc(plant.availability||'Needs operator review')}</small></div>
    <div class="plant-popup-kpis">
      <div><span>Current output</span><b>${output==null?'Not connected':output.toFixed(1)+' MW'}</b></div>
      <div><span>Project capacity</span><b>${capacity==null?'—':capacity.toFixed(1)} MW</b></div>
    </div>
    <div class="plant-utilization"><div><span>Capacity in use</span><b>${utilization==null?'—':utilization.toFixed(0)+'%'}</b></div><div class="plant-progress"><i style="width:${utilization==null?0:utilization.toFixed(1)}%;--status:${statusColor(plant.status)}"></i></div></div>
    ${plant.status_reasons?.length?`<div class="plant-popup-note"><i class="fa-solid fa-circle-info"></i>${esc(plant.status_reasons.join(' · '))}</div>`:''}
    <small class="plant-data-note"><i class="fa-solid fa-database"></i> ${plant.operational_values_synthetic?'Real Jordan project identity · synthetic operational dataset':'Operational data source'}</small>
  </div>`;
}

function createBaseMap(id,mini=false){
  const instance=L.map(id,{
    zoomControl:!mini,
    attributionControl:!mini,
    scrollWheelZoom:!mini
  }).setView([31.1,36.0],6.5);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
    maxZoom:18,
    attribution:'&copy; OpenStreetMap contributors'
  }).addTo(instance);
  return instance;
}

async function fetchPlants(){
  return (await api('/api/grid/plants')).plants||[];
}

function populate(target,rows,small=false){
  target.clearLayers();
  const points=[];
  rows.forEach(plant=>{
    if(plant.lat==null||plant.lon==null)return;
    const point=[plant.lat,plant.lon];
    points.push(point);
    L.marker(point,{icon:markerIcon(plant,small),keyboard:true,title:plant.name||'Solar plant'})
      .bindPopup(popup(plant),{maxWidth:330,minWidth:280})
      .addTo(target);
  });
  return points;
}

export async function loadMap(){
  if(!map){
    map=createBaseMap('gridMap');
    layer=L.layerGroup().addTo(map);
  }
  try{
    const rows=await fetchPlants();
    const points=populate(layer,rows);
    if(points.length)map.fitBounds(points,{padding:[55,55],maxZoom:8});
    document.querySelector('#mapMessage').textContent=points.length?`${points.length} dataset-backed solar projects shown · all participate in planning and monitoring`:'No mapped solar plants';
    document.querySelector('#mapUpdated').textContent=`Updated ${new Date().toLocaleTimeString()}`;
    setTimeout(()=>map.invalidateSize(),80);
  }catch(error){
    document.querySelector('#mapMessage').textContent=`Map unavailable: ${error.message}`;
  }
}

export async function loadMiniMap(){
  const el=document.querySelector('#miniMap');
  if(!el)return;
  try{
    const rows=await fetchPlants();
    if(!rows.length){
      el.innerHTML='<div class="map-placeholder"><i class="fa-regular fa-map"></i><span>No solar plants are available in the active dataset.</span></div>';
      return;
    }
    if(!miniMap){
      el.innerHTML='';
      miniMap=createBaseMap('miniMap',true);
      miniLayer=L.layerGroup().addTo(miniMap);
    }
    const points=populate(miniLayer,rows,true);
    if(points.length)miniMap.fitBounds(points,{padding:[28,28],maxZoom:7});
    setTimeout(()=>miniMap.invalidateSize(),80);
  }catch{
    el.innerHTML='<div class="map-placeholder"><i class="fa-solid fa-triangle-exclamation"></i><span>Map preview is temporarily unavailable.</span></div>';
  }
}

export function startMapPolling(){
  clearInterval(timer);
  timer=setInterval(loadMap,30000);
}
