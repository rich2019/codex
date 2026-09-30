<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import nacl from 'tweetnacl'

type User = { id:number; username:string; role:string; active:boolean; password_change_required:boolean; created_at:string }
type Task = { id:string; user_id:number; prompt:string; model:string|null; reasoning_effort:string|null; status:string; codex_session_id?:string|null; final_output:string; error:string; created_at:string; updated_at:string; diff?:string; events?:any[] }
type ModelChoice = { id:string; name:string; default_effort:string|null; reasoning_efforts:string[] }
type ModelCatalog = { available:boolean; default_model:string|null; models:ModelChoice[]; stale?:boolean; message?:string }
type UiEvent = { seq?:number; type:string; data:any; raw:any }
type Conversation = { key:string; user_id:number; session_id:string|null; turns:Task[]; title:string; updated_at:string; latest:Task }
const setupNeeded=ref(false), loading=ref(true), user=ref<User|null>(null), csrf=ref(''), error=ref('')
const loginForm=ref({username:'',password:''}), setupForm=ref({token:'',username:'admin',password:''}), passwordForm=ref({current_password:'',new_password:''})
const tasks=ref<Task[]>([]), prompt=ref(''), currentEvents=ref<UiEvent[]>([]), currentDiff=ref(''), system=ref<any>({}), users=ref<User[]>([]), activeTab=ref('tasks'), userForm=ref({username:'',role:'user'})
const modelCatalog=ref<ModelCatalog>({available:false,default_model:null,models:[]}), selectedModel=ref(''), selectedEffort=ref(''), selectedTaskId=ref(''), showDiff=ref(false)
const pendingUploads=ref<any[]>([]), taskFiles=ref<any[]>([]), uploading=ref(false)
const fileInput=ref<HTMLInputElement|null>(null), hostStatus=ref<Record<string,any>>({}), includeServerSnapshot=ref(false)
let source:EventSource|null=null, refreshTimer:any=null, codexPoll:any=null
const deviceLogin=ref<any>({running:false,authenticated:false,device_url:'',user_code:'',message:''})
const isAdmin=computed(()=>user.value?.role==='admin')
const conversations=computed<Conversation[]>(()=>{
  const grouped=new Map<string,Task[]>()
  for(const task of [...tasks.value].sort((a,b)=>a.created_at.localeCompare(b.created_at))){
    const session=task.codex_session_id||null
    const key=`${task.user_id}:${session||task.id}`
    const turns=grouped.get(key)||[]
    turns.push(task)
    grouped.set(key,turns)
  }
  return [...grouped.entries()].map(([key,turns])=>{
    const latest=turns[turns.length-1]
    return {key,user_id:latest.user_id,session_id:latest.codex_session_id||null,turns,title:turns[0].prompt||'新建对话',updated_at:latest.updated_at,latest}
  }).sort((a,b)=>b.updated_at.localeCompare(a.updated_at))
})
const currentConversation=computed(()=>conversations.value.find(c=>c.turns.some(t=>t.id===selectedTaskId.value))||null)
const currentTurns=computed(()=>currentConversation.value?.turns||[])
const latestTurn=computed(()=>currentConversation.value?.latest||null)
const conversationBusy=computed(()=>!!latestTurn.value&&['queued','running','cancel_requested'].includes(latestTurn.value.status))
const effectiveModel=computed(()=>selectedModel.value||modelCatalog.value.default_model||'')
const selectedModelInfo=computed(()=>modelCatalog.value.models.find(m=>m.id===effectiveModel.value)||null)
const effortOptions=computed(()=>selectedModelInfo.value?.reasoning_efforts||[])
const activityItems=computed(()=>currentEvents.value.map(e=>({event:e,label:eventLabel(e)})).filter(x=>x.label).slice(-12))
const rawEventLog=computed(()=>JSON.stringify(currentEvents.value.map(({seq,type,data})=>({seq,type,data})),null,2))
const errorLabels:Record<string,string>={password:'密码',new_password:'新密码',current_password:'当前密码',token:'初始化令牌',username:'用户名',role:'角色',prompt:'任务描述'}
function formatError(value:any):string{
  if(value instanceof Error)return value.message||'请求失败'
  if(typeof value==='string')return value
  if(value==null)return '请求失败'
  if(Array.isArray(value))return value.map(formatError).filter(Boolean).join('；')||'请求失败'
  if(typeof value==='object'){
    if(typeof value.detail==='string')return value.detail
    if(value.detail)return formatError(value.detail)
    if(typeof value.message==='string')return value.message
    if(typeof value.msg==='string'){
      const loc=Array.isArray(value.loc)?value.loc.filter((x:any)=>x!=='body'):[]
      const field=String(loc[loc.length-1]||'')
      const label=errorLabels[field]||field
      if(value.type==='missing'||value.type==='string_too_short')return `${label||'参数'}不能为空`
      if(value.type==='string_too_long')return `${label||'参数'}过长`
      if(value.type==='string_pattern_mismatch')return `${label||'参数'}格式不正确`
      return label?`${label}${value.msg}`:value.msg
    }
    try{return JSON.stringify(value)}catch{return '请求失败'}
  }
  return String(value)
}
async function api(path:string, options:RequestInit={}) {
  const headers=new Headers(options.headers||{})
  if(options.body && !headers.has('Content-Type')) headers.set('Content-Type','application/json')
  if(csrf.value && !['GET','HEAD'].includes((options.method||'GET').toUpperCase())) headers.set('X-CSRF-Token',csrf.value)
  const r=await fetch(path,{...options,headers,credentials:'same-origin'})
  const text=await r.text()
  let body:any=null
  try{body=text?JSON.parse(text):null}catch{body={detail:text||r.statusText}}
  if(!r.ok) throw new Error(formatError(body)||`HTTP ${r.status}`)
  return body
}
function bytesToBase64(bytes:Uint8Array):string{
  let binary=''
  for(const byte of bytes)binary+=String.fromCharCode(byte)
  return btoa(binary)
}
function base64ToBytes(value:string):Uint8Array{
  const binary=atob(value)
  const bytes=new Uint8Array(binary.length)
  for(let i=0;i<binary.length;i++)bytes[i]=binary.charCodeAt(i)
  return bytes
}
async function encryptSecret(payload:Record<string,string>){
  const challenge=await api('/api/auth/crypto/challenge')
  const ephemeral=nacl.box.keyPair()
  const nonce=nacl.randomBytes(nacl.box.nonceLength)
  const plaintext=new TextEncoder().encode(JSON.stringify({...payload,challenge_id:challenge.challenge_id}))
  const ciphertext=nacl.box(plaintext,nonce,base64ToBytes(challenge.public_key),ephemeral.secretKey)
  return {key_id:challenge.key_id,challenge_id:challenge.challenge_id,ephemeral_public_key:bytesToBase64(ephemeral.publicKey),nonce:bytesToBase64(nonce),ciphertext:bytesToBase64(ciphertext)}
}
async function init(){
  loading.value=true
  try {
    const s=await api('/api/setup/status'); setupNeeded.value=s.needs_admin
    if(!setupNeeded.value){try{const m=await api('/api/auth/me');user.value=m.user;csrf.value=m.csrf_token}catch{user.value=null}}
    if(user.value && !user.value.password_change_required) await loadAll()
  } catch(e:any){error.value=formatError(e);ElMessage.error(error.value)} finally{loading.value=false}
}
async function bootstrap(){try{const secret=await encryptSecret({token:setupForm.value.token,password:setupForm.value.password});await api('/api/setup/bootstrap',{method:'POST',body:JSON.stringify({username:setupForm.value.username,secret})});ElMessage.success('管理员已创建，请登录');setupNeeded.value=false;setupForm.value.token=''}catch(e:any){ElMessage.error(formatError(e))}}
async function login(){try{const secret=await encryptSecret({password:loginForm.value.password});const x=await api('/api/auth/login',{method:'POST',body:JSON.stringify({username:loginForm.value.username,secret})});user.value=x.user;csrf.value=x.csrf_token;loginForm.value.password='';if(user.value?.password_change_required){passwordForm.value.current_password='';return}await loadAll()}catch(e:any){ElMessage.error(formatError(e))}}
async function changePassword(){try{const secret=await encryptSecret(passwordForm.value);const x=await api('/api/auth/password',{method:'POST',body:JSON.stringify({secret})});user.value=x.user;passwordForm.value={current_password:'',new_password:''};ElMessage.success('密码已更新');await loadAll()}catch(e:any){ElMessage.error(formatError(e))}}
async function logout(){try{await api('/api/auth/logout',{method:'POST'})}catch{}if(source)source.close();user.value=null;csrf.value='';selectedTaskId.value='';tasks.value=[]}
async function loadAll(){await Promise.all([loadTasks(),loadSystem(),loadUploads()]);void loadModels();if(isAdmin.value)await loadUsers();if(!refreshTimer)refreshTimer=setInterval(()=>{loadTasks();loadSystem()},5000)}
async function loadTasks(){try{tasks.value=await api('/api/tasks')}catch(e:any){if(e.message.includes('登录'))logout()}}
async function loadUploads(){try{pendingUploads.value=await api('/api/uploads')}catch{}}
async function loadSystem(){try{system.value=await api('/api/system/status')}catch{}}
async function loadHostStatus(){try{hostStatus.value=await api('/api/admin/hosts/status');includeServerSnapshot.value=true;ElMessage.success('已采集两台服务器只读状态；发送下一条消息时会附加快照')}catch(e:any){ElMessage.error(formatError(e))}}
async function loadModels(){try{modelCatalog.value=await api('/api/codex/models')}catch{modelCatalog.value={available:false,default_model:null,models:[],message:'模型目录不可用；仍可使用 Codex 默认设置'}}}
async function loadUsers(){try{users.value=await api('/api/admin/users')}catch(e:any){ElMessage.error(formatError(e))}}
async function refreshCodexLogin(){try{deviceLogin.value=await api('/api/admin/codex/login/status');if(deviceLogin.value.authenticated){if(codexPoll)clearInterval(codexPoll);codexPoll=null;await loadSystem();ElMessage.success('ChatGPT 账号已连接')}}catch{}}
async function startCodexLogin(){try{deviceLogin.value=await api('/api/admin/codex/login/start',{method:'POST'});if(!codexPoll)codexPoll=setInterval(refreshCodexLogin,2000)}catch(e:any){ElMessage.error(formatError(e))}}
async function cancelCodexLogin(){try{deviceLogin.value=await api('/api/admin/codex/login/cancel',{method:'POST'});if(codexPoll)clearInterval(codexPoll);codexPoll=null}catch(e:any){ElMessage.error(formatError(e))}}
function newChat(){if(source){source.close();source=null}selectedTaskId.value='';prompt.value='';currentEvents.value=[];currentDiff.value='';taskFiles.value=[];selectedModel.value='';selectedEffort.value='';showDiff.value=false}
function changeModel(model:string){selectedModel.value=model;selectedEffort.value=modelCatalog.value.models.find(m=>m.id===model)?.default_effort||''}
function normalizeEvent(raw:any):UiEvent{const event=raw?.payload||raw;return {seq:raw?.seq??event?.seq,type:event?.type||raw?.event_type||'event',data:event?.data??{},raw}}
function eventLabel(event:UiEvent):string{
  const item=event.data?.item||{}
  if(item.type==='agent_message')return ''
  if(event.type==='worker.started')return '正在准备独立工作区'
  if(event.type==='thread.started')return '已连接 Codex 会话'
  if(event.type==='turn.started')return 'Codex 正在处理'
  if(event.type==='turn.completed')return '本轮已完成'
  if(event.type==='turn.failed')return '本轮执行失败'
  if(event.type==='turn.cancelled')return '本轮已取消'
  if(item.type==='command_execution')return '执行了终端命令'
  if(item.type==='file_change')return '检查或修改了文件'
  if(event.type==='item.started')return '正在执行工具操作'
  if(event.type==='item.completed')return '工具操作已完成'
  if(event.type==='log')return event.data?.text?'运行日志':''
  return ''
}
async function refreshConversationDetail(){
  const latest=currentConversation.value?.latest
  if(!latest)return
  try{
    const detail=await api(`/api/tasks/${latest.id}`)
    currentDiff.value=detail.diff||''
    currentEvents.value=(detail.events||[]).map(normalizeEvent)
    taskFiles.value=await api(`/api/tasks/${latest.id}/files`).catch(()=>[])
  }catch{}
}
async function openConversation(taskId:string){
  if(source){source.close();source=null}
  selectedTaskId.value=taskId
  currentEvents.value=[];currentDiff.value=''
  const conversation=currentConversation.value
  if(!conversation)return
  selectedModel.value=conversation.latest.model||''
  selectedEffort.value=conversation.latest.reasoning_effort||''
  await refreshConversationDetail()
  const latest=currentConversation.value?.latest
  if(!latest||!['queued','running','cancel_requested'].includes(latest.status))return
  const after=currentEvents.value.reduce((max,event)=>Math.max(max,event.seq||0),0)
  source=new EventSource(`/api/tasks/${latest.id}/events?after=${after}`,{withCredentials:true})
  source.onmessage=(message)=>{try{currentEvents.value.push(normalizeEvent(JSON.parse(message.data)));loadTasks()}catch{}}
  source.addEventListener('end',async()=>{source?.close();source=null;await loadTasks();await refreshConversationDetail()})
  source.onerror=()=>{source?.close();source=null;loadTasks()}
}
async function sendMessage(){
  const message=prompt.value.trim()
  if(!message||conversationBusy.value)return
    const settings={model:selectedModel.value||null,reasoning_effort:selectedEffort.value||null,upload_ids:pendingUploads.value.map(f=>f.id),include_server_snapshot:includeServerSnapshot.value}
  try{
    const conversation=currentConversation.value
    let task:Task
    if(conversation?.session_id){
      task=await api(`/api/tasks/${conversation.latest.id}/resume`,{method:'POST',body:JSON.stringify({prompt:message,...settings})})
    }else{
      task=await api('/api/tasks',{method:'POST',body:JSON.stringify({prompt:message,...settings})})
    }
    prompt.value=''
    pendingUploads.value=[]
    includeServerSnapshot.value=false
    await loadTasks()
    await openConversation(task.id)
  }catch(e:any){ElMessage.error(formatError(e))}
}
async function onFilesSelected(event:Event){
  const input=event.target as HTMLInputElement
  const files=Array.from(input.files||[]);input.value=''
  if(!files.length)return
  uploading.value=true
  try{
    for(const file of files){
      if(file.size>25*1024*1024)throw new Error(`${file.name} 超过 25 MB`)
      const form=new FormData();form.append('file',file)
      const headers=new Headers();if(csrf.value)headers.set('X-CSRF-Token',csrf.value)
      const response=await fetch('/api/uploads',{method:'POST',body:form,headers,credentials:'same-origin'})
      const result=await response.json().catch(()=>({detail:'上传失败'}))
      if(!response.ok)throw new Error(formatError(result))
      pendingUploads.value.push(result)
    }
    ElMessage.success(`已上传 ${files.length} 个文件`)
  }catch(e:any){ElMessage.error(formatError(e))}finally{uploading.value=false}
}
async function removeUpload(file:any){try{await api(`/api/uploads/${file.id}`,{method:'DELETE'});pendingUploads.value=pendingUploads.value.filter(f=>f.id!==file.id)}catch(e:any){ElMessage.error(formatError(e))}}
function downloadHref(filePath:string){return `/api/tasks/${latestTurn.value?.id}/files/${filePath.split('/').map(encodeURIComponent).join('/')}`}
async function cancelTask(){const task=latestTurn.value;if(!task)return;try{await api(`/api/tasks/${task.id}/cancel`,{method:'POST'});ElMessage.success('已请求取消');await loadTasks()}catch(e:any){ElMessage.error(formatError(e))}}
async function addUser(){if(!userForm.value.username.trim())return;try{const x=await api('/api/admin/users',{method:'POST',body:JSON.stringify(userForm.value)});await loadUsers();await ElMessageBox.alert(`临时密码：${x.temporary_password}\n请安全复制给用户；首次登录必须修改。`,'用户已创建',{confirmButtonText:'知道了'}) ;userForm.value={username:'',role:'user'}}catch(e:any){ElMessage.error(formatError(e))}}
async function toggleUser(u:User){try{await api(`/api/admin/users/${u.id}?active=${!u.active}&role=${u.role}`,{method:'PATCH'});await loadUsers()}catch(e:any){ElMessage.error(formatError(e))}}
async function resetUser(u:User){try{await ElMessageBox.confirm(`重置 ${u.username} 的登录密码？`,'确认');const x=await api(`/api/admin/users/${u.id}/reset-password`,{method:'POST'});await ElMessageBox.alert(`临时密码：${x.temporary_password}\n用户下次登录后必须修改。`,'密码已重置')}catch(e:any){if(e!=='cancel')ElMessage.error(formatError(e))}}
function statusType(s:string){return ({queued:'info',running:'warning',cancel_requested:'warning',succeeded:'success',failed:'danger',cancelled:'info',interrupted:'danger'} as any)[s]||'info'}
function statusLabel(s:string){return ({queued:'排队中',running:'运行中',cancel_requested:'取消中',succeeded:'已完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'} as any)[s]||s}
function time(v:string){return v?new Date(v).toLocaleString():'—'}
function uptime(seconds:number){if(!Number.isFinite(seconds))return '—';const days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600);return `${days}天 ${hours}小时`}
function handleComposerKeydown(event:KeyboardEvent){if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();void sendMessage()}}
onMounted(init)
onUnmounted(()=>{if(source)source.close();if(refreshTimer)clearInterval(refreshTimer);if(codexPoll)clearInterval(codexPoll)})
</script>

<template>
  <div v-if="loading" class="login-wrap"><el-card>正在连接控制台…</el-card></div>
  <div v-else-if="setupNeeded" class="login-wrap"><div class="login-card"><div class="brand"><span class="brand-mark">C</span> Codex 控制台</div><h1 style="margin-top:24px">首次初始化</h1><p>使用服务器生成的一次性初始化令牌创建管理员。管理员密码不能为空。</p><el-form label-position="top"><el-form-item label="初始化令牌"><el-input v-model="setupForm.token" show-password /></el-form-item><el-form-item label="管理员用户名"><el-input v-model="setupForm.username" /></el-form-item><el-form-item label="管理员密码"><el-input v-model="setupForm.password" type="password" show-password /></el-form-item><el-button type="primary" style="width:100%" @click="bootstrap">创建管理员</el-button></el-form></div></div>
  <div v-else-if="!user" class="login-wrap"><div class="login-card"><div class="brand"><span class="brand-mark">C</span> Codex 控制台</div><h1 style="margin-top:24px">登录</h1><p>使用管理员或已创建的用户账号登录。</p><el-form label-position="top" @submit.prevent="login"><el-form-item label="用户名"><el-input v-model="loginForm.username" autocomplete="username" /></el-form-item><el-form-item label="密码"><el-input v-model="loginForm.password" type="password" show-password autocomplete="current-password" @keyup.enter="login" /></el-form-item><el-button type="primary" style="width:100%" @click="login">登录</el-button></el-form></div></div>
  <div v-else-if="user.password_change_required" class="login-wrap"><div class="login-card"><div class="brand"><span class="brand-mark">C</span> Codex 控制台</div><h1 style="margin-top:24px">设置新密码</h1><p>请输入新的非空密码后继续。</p><el-form label-position="top"><el-form-item label="新密码"><el-input v-model="passwordForm.new_password" type="password" show-password /></el-form-item><el-button type="primary" style="width:100%" @click="changePassword">更新密码</el-button></el-form></div></div>
  <div v-else class="shell">
    <header class="topbar"><div class="brand"><span class="brand-mark">C</span> Codex 控制台</div><div class="top-actions"><el-tag :type="system.codex?.authenticated?'success':'warning'">{{system.codex?.authenticated?'Codex 已登录':'Codex 未登录'}}</el-tag><span>{{user.username}} · {{user.role==='admin'?'管理员':'用户'}}</span><el-button text @click="logout">退出</el-button></div></header>
    <main class="layout" :class="{'conversation-page':activeTab==='tasks'}"><div class="tabs"><el-button :type="activeTab==='tasks'?'primary':'default'" @click="activeTab='tasks'">对话</el-button><el-button v-if="isAdmin" :type="activeTab==='users'?'primary':'default'" @click="activeTab='users';loadUsers()">用户管理</el-button></div>
      <template v-if="activeTab==='tasks'"><div v-if="isAdmin" class="card codex-login-card"><div class="section-head"><div><h2>ChatGPT / Codex 账号</h2><div class="muted">使用你的 Plus 账号进行设备登录；无需 API key。授权在 ChatGPT 官方页面完成。</div></div><el-button v-if="!deviceLogin.running" type="primary" @click="startCodexLogin">{{system.codex?.authenticated?'重新登录':'连接 ChatGPT 账号'}}</el-button><el-button v-else type="danger" plain @click="cancelCodexLogin">取消登录</el-button></div><div v-if="deviceLogin.running || deviceLogin.authenticated" class="device-login"><div>{{deviceLogin.message||'等待账号授权'}}</div><div v-if="deviceLogin.user_code" class="device-code">{{deviceLogin.user_code}}</div><el-link v-if="deviceLogin.device_url" :href="deviceLogin.device_url" target="_blank" rel="noopener noreferrer" type="primary">打开 OpenAI 设备授权页面</el-link><div class="muted">在授权页面输入上方代码。不要把设备代码发给他人。</div></div></div><div class="status-line"><span class="dot" :class="system.codex?.authenticated?'ok':'bad'"></span><span>{{system.codex?.message||'正在检查 Codex 状态'}}</span><el-tag v-if="system.workspace_git" size="small" type="success">Git 工作区已配置</el-tag><el-tag v-else size="small" type="warning">尚未配置 Git 仓库</el-tag><el-button v-if="isAdmin" size="small" plain @click="loadHostStatus">检查服务器</el-button><el-tag v-if="includeServerSnapshot" type="success" size="small">本轮将附加诊断快照</el-tag></div>
        <div v-if="isAdmin&&Object.keys(hostStatus).length" class="host-status-grid"><div v-for="(host,name) in hostStatus" :key="name" class="host-status-card"><b>{{name==='ecs'?'ECS':'轻量服务器'}}</b><el-tag size="small" :type="host.online?'success':'danger'">{{host.online?'在线':'离线'}}</el-tag><template v-if="host.online"><span>CPU {{host.cpu_percent}}% · 内存 {{host.memory?.used_percent}}% · 磁盘 {{host.disk?.used_percent}}%</span><span>负载 {{host.load_average?.slice(0,3).join(' / ')}} · 已运行 {{uptime(host.uptime_seconds)}}</span><span v-for="(state,service) in host.services" :key="service">{{service}} {{state.healthy?'正常':'异常'}}</span></template><span v-else>状态采集器暂不可达</span></div></div>
        <div class="chat-workspace" :class="{'has-diff':showDiff}">
          <aside class="conversation-sidebar">
            <div class="conversation-sidebar-head"><h2>会话</h2><el-button type="primary" size="small" @click="newChat">新建</el-button></div>
            <div v-if="conversations.length" class="conversation-list">
              <button v-for="conversation in conversations" :key="conversation.key" class="conversation-item" :class="{selected:conversation.turns.some(t=>t.id===selectedTaskId)}" @click="openConversation(conversation.latest.id)">
                <span class="conversation-title">{{conversation.title}}</span>
                <span class="conversation-meta"><el-tag :type="statusType(conversation.latest.status)" size="small">{{statusLabel(conversation.latest.status)}}</el-tag><span>{{time(conversation.updated_at)}}</span></span>
                <span v-if="isAdmin" class="conversation-meta">用户 #{{conversation.user_id}}</span>
              </button>
            </div>
            <el-empty v-else description="还没有会话" :image-size="72" />
          </aside>

          <section class="chat-main">
            <div v-if="currentConversation" class="chat-header">
              <div><h2>{{currentConversation.title}}</h2><span class="muted">{{currentTurns.length}} 轮对话 · {{statusLabel(latestTurn?.status||'')}}</span></div>
              <div class="chat-header-actions">
                <el-button v-if="conversationBusy" type="danger" plain @click="cancelTask">取消本轮</el-button>
                <el-button :type="showDiff?'primary':'default'" plain @click="showDiff=!showDiff">文件改动</el-button>
              </div>
            </div>
            <div v-if="currentConversation" class="message-list">
              <article v-for="turn in currentTurns" :key="turn.id" class="turn-block">
                <div class="message user-message"><div class="message-role">你</div><div class="message-text">{{turn.prompt}}</div></div>
                <div v-if="turn.final_output" class="message assistant-message"><div class="message-role">Codex</div><div class="message-text">{{turn.final_output}}</div></div>
                <div v-else-if="turn.status==='failed'" class="message assistant-message error-message"><div class="message-role">Codex 执行失败</div><div class="message-text">{{turn.error||'任务失败'}}</div></div>
                <div v-if="turn.id===latestTurn?.id&&conversationBusy" class="activity-card">
                  <div class="activity-title"><span class="activity-pulse"></span>{{statusLabel(turn.status)}}<span v-if="!activityItems.length">Codex 正在处理本轮请求…</span></div>
                  <div v-for="(activity,index) in activityItems" :key="`${activity.event.seq||index}-${activity.event.type}`" class="activity-row">{{activity.label}}</div>
                </div>
                <details v-if="turn.id===latestTurn?.id&&currentEvents.length" class="run-log">
                  <summary>运行记录（{{currentEvents.length}} 条事件）</summary>
                  <pre>{{rawEventLog}}</pre>
                </details>
                <div class="turn-footer"><el-tag :type="statusType(turn.status)" size="small">{{statusLabel(turn.status)}}</el-tag><span>{{time(turn.created_at)}}</span><span v-if="turn.model">{{turn.model}}</span><span v-if="turn.reasoning_effort">{{turn.reasoning_effort}}</span></div>
              </article>
            </div>
            <div v-else class="chat-welcome"><div class="welcome-mark">C</div><h2>开始新的 Codex 对话</h2><p>描述你希望在 Git 工作区中完成的工作。Codex 会在独立 worktree 中运行。</p></div>

            <div class="composer-wrap">
              <div class="model-controls">
                <label>模型</label>
                <el-select v-model="selectedModel" :disabled="!modelCatalog.available" size="small" style="width:220px" @change="changeModel">
                  <el-option label="Codex 默认" value="" />
                  <el-option v-for="model in modelCatalog.models" :key="model.id" :label="model.name" :value="model.id" />
                </el-select>
                <label>推理强度</label>
                <el-select v-model="selectedEffort" :disabled="!effortOptions.length" size="small" style="width:150px">
                  <el-option label="Codex 默认" value="" />
                  <el-option v-for="effort in effortOptions" :key="effort" :label="effort" :value="effort" />
                </el-select>
                <span v-if="modelCatalog.stale" class="muted">模型目录缓存</span>
                <span v-else-if="!modelCatalog.available" class="muted">{{modelCatalog.message||'可继续使用 Codex 默认模型'}}</span>
              </div>
              <div v-if="pendingUploads.length" class="upload-chips"><el-tag v-for="file in pendingUploads" :key="file.id" closable @close="removeUpload(file)">{{file.name}} · {{(file.size/1048576).toFixed(1)}} MB</el-tag></div>
              <el-input v-model="prompt" class="chat-input" type="textarea" :autosize="{minRows:2,maxRows:7}" maxlength="20000" placeholder="给 Codex 发送消息…（Enter 发送，Shift+Enter 换行）" @keydown="handleComposerKeydown" />
              <input ref="fileInput" class="hidden-file-input" type="file" multiple @change="onFilesSelected" />
              <div class="composer-footer"><span class="muted">{{system.workspace_git?'任务在独立 Git worktree 中运行；不会自动合并或部署。':'尚未配置 Git 工作区'}}</span><div class="composer-actions"><el-button plain :loading="uploading" :disabled="conversationBusy" @click="fileInput?.click()">添加文件</el-button><el-button type="primary" :disabled="!prompt.trim()||conversationBusy||!system.codex?.authenticated||!system.workspace_git" @click="sendMessage">{{conversationBusy?'本轮运行中…':'发送'}}</el-button></div></div>
            </div>
          </section>

          <aside v-if="showDiff" class="diff-sidebar">
            <div class="diff-header"><div><h2>文件改动</h2><span class="muted">相对仓库 HEAD 的补丁预览</span></div><el-button text @click="showDiff=false">关闭</el-button></div>
            <pre class="diff-content">{{currentDiff||'本会话尚无文件改动'}}</pre>
            <div class="file-downloads"><div class="file-download-head"><h3>工作区文件</h3><a v-if="latestTurn" :href="`/api/tasks/${latestTurn.id}/files.zip`">下载 ZIP</a></div><div v-if="taskFiles.length" class="file-download-list"><div v-for="file in taskFiles" :key="file.path" class="file-download-row"><span :title="file.path">{{file.path}}</span><a :href="downloadHref(file.path)" download>下载</a></div></div><span v-else class="muted">任务运行后可下载文件</span></div>
            <p class="muted">改动保存在独立 worktree 中，不会自动合并或部署。</p>
          </aside>
        </div></template>
      <section v-else class="card"><div class="section-head"><h2>用户管理</h2><span class="muted">不开放公开注册；普通用户仅查看自己的任务</span></div><div style="display:flex;gap:10px;max-width:600px;margin-bottom:20px"><el-input v-model="userForm.username" placeholder="新用户名"/><el-select v-model="userForm.role" style="width:150px"><el-option label="普通用户" value="user"/><el-option label="管理员" value="admin"/></el-select><el-button type="primary" @click="addUser">创建用户</el-button></div><el-table :data="users" class="user-table"><el-table-column prop="username" label="用户名"/><el-table-column prop="role" label="角色"><template #default="s">{{s.row.role==='admin'?'管理员':'普通用户'}}</template></el-table-column><el-table-column label="状态"><template #default="s"><el-tag :type="s.row.active?'success':'info'">{{s.row.active?'启用':'停用'}}</el-tag></template></el-table-column><el-table-column prop="created_at" label="创建时间" min-width="180"><template #default="s">{{time(s.row.created_at)}}</template></el-table-column><el-table-column label="操作" min-width="220"><template #default="s"><el-button size="small" @click="resetUser(s.row)">重置密码</el-button><el-button size="small" :type="s.row.active?'danger':'success'" @click="toggleUser(s.row)">{{s.row.active?'停用':'启用'}}</el-button></template></el-table-column></el-table></section>
    </main>
  </div>
</template>
