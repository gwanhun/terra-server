// terra-server 웹 콘솔 공용 스크립트 — index.html(메인) · beta.html(베타 테스터) · live.html(라이브 대시보드)
//
// 왜 분리했나(2026-10-21): 베타 테스터 패널과 라이브 대시보드를 별도 페이지로 떼면서 esc/toast/OTA 모달/
// 베타 계정 인증이 세 곳에서 똑같이 필요해졌다. 각 페이지는 자기 `CONFIG`(apiBase·supabase 정보)를
// 선언하고, 여기 함수들은 호출 시점의 전역 CONFIG 를 읽는다 (classic script 전역 공유).
//
// 의존: supabase-js UMD(window.supabase) 가 먼저 로드돼 있어야 getBatchClient 가 동작.

// ─────────────────────────────────────────────────────────────────────────
// 설정 — localStorage 의 apiBase + 백엔드 /web-config 의 supabase 정보
// ─────────────────────────────────────────────────────────────────────────
// 페이지가 api 서버 자체에서 서빙되면 같은 origin 사용 → CORS 무관.
// 로컬 정적 서버(localhost:5500 등)에서 띄운 거면 원격 API 로 fallback.
const DEFAULT_API_BASE = (
  location.protocol === 'https:' || location.hostname !== 'localhost'
) ? location.origin : 'https://api.terra-server.uk';

// localStorage 'terra-config' 의 apiBase (없으면 DEFAULT_API_BASE). 세 페이지가 같은 키를 쓴다.
function loadSavedApiBase() {
  try {
    const saved = JSON.parse(localStorage.getItem('terra-config') || '{}');
    if (saved.apiBase) return saved.apiBase;
  } catch (e) { /* 깨진 값 → 기본값 */ }
  return DEFAULT_API_BASE;
}

async function fetchRemoteConfig(apiBase) {
  // 백엔드의 /web-config — SUPABASE_URL + publishable key 자동 노출
  const url = apiBase + '/web-config';
  const res = await fetch(url);
  if (!res.ok) throw new Error(`/web-config 응답 ${res.status}`);
  const body = await res.json();
  if (!body.supabaseUrl || !body.supabasePublishableKey) {
    throw new Error('백엔드 .env 에 SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY 누락');
  }
  return { supabaseUrl: body.supabaseUrl, supabasePublishable: body.supabasePublishableKey };
}

// ─────────────────────────────────────────────────────────────────────────
// 상단 네비 — #topnav 에 그린다. active: 'index' | 'beta' | 'live'
// ─────────────────────────────────────────────────────────────────────────
const NAV_PAGES = [
  { key: 'index', href: 'index.html', label: '🏠 메인 콘솔' },
  { key: 'beta', href: 'beta.html', label: '🎫 베타 테스터' },
  { key: 'live', href: 'live.html', label: '🎥 라이브 대시보드' },
];
function renderTopNav(active, apiBase) {
  const el = document.getElementById('topnav');
  if (!el) return;
  const base = apiBase || loadSavedApiBase();
  const host = base.replace(/^https?:\/\//, '');
  const envCls = base.includes('localhost') ? 'local' : 'prod';
  el.className = 'topnav';
  el.innerHTML = `<span class="brand">🦎 Terra</span>`
    + NAV_PAGES.map(p => `<a class="nav${p.key === active ? ' active' : ''}" href="${p.href}">${p.label}</a>`).join('')
    + `<span class="spacer"></span>`
    + `<span class="env ${envCls}" title="현재 API Base (localStorage terra-config)">${esc(host)}</span>`
    + `<span class="docs-links" id="docs-links" title="처음 클릭 시 Basic Auth — .env 의 DOCS_BASIC_USER/PASS"></span>`;
}

// 헤더의 docs 링크 — 현재 API Base 기준으로 동적 생성.
// /docs, /redoc 은 Basic Auth 가드 (.env DOCS_BASIC_USER/PASS) — 첫 클릭 시 prompt.
function renderDocsLinks(apiBase) {
  const base = apiBase || loadSavedApiBase();
  const el = document.getElementById('docs-links');
  if (!el) return;
  el.innerHTML = `
    <a href="${base}/docs" target="_blank" rel="noopener">📘 Swagger</a>
    <a href="${base}/redoc" target="_blank" rel="noopener">📕 ReDoc</a>
    <a href="${base}/openapi.json" target="_blank" rel="noopener">📋 OpenAPI</a>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// 유틸
// ─────────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// 최근 API 응답 로그 — #log <pre> 가 있는 페이지에서만 표시(없으면 조용히 무시).
function log(obj) {
  const el = document.getElementById('log');
  if (!el) return;
  el.innerText = JSON.stringify(obj, null, 2) + '\n\n' + el.innerText;
  el.scrollTop = 0;
}

function toast(msg, cls) {
  const t = document.createElement('div');
  t.className = 'toast ' + (cls || '');
  t.innerText = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

// UTC ISO → KST "MM/DD HH:MM"
function fmtKst(iso) {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString('ko-KR', {
      timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  } catch (e) { return iso.slice(5, 16); }
}

// created_at(UTC ISO) → "MM-DD HH:MM:SS" KST. 문자열을 그대로 자르면 UTC 로 보여서(2026-10-16) +9h 로 변환.
function fmtKstSec(iso) {
  const t = Date.parse(iso || '');
  if (Number.isNaN(t)) return (iso || '').slice(5, 19).replace('T', ' ');
  return new Date(t + 9 * 3600e3).toISOString().slice(5, 19).replace('T', ' ');
}

// sys_state / clip_stats.sys → 한 줄 텍스트 (가동시간·리셋 사유·힙·RSSI·AP·net 진단)
function sysStateText(sy) {
  const up = sy.uptime_s >= 0 ? (sy.uptime_s < 3600 ? Math.round(sy.uptime_s/60)+'m' : (sy.uptime_s/3600).toFixed(1)+'h') : '?';
  const resetBad = sy.reset && !/^(POWERON|SW:(rotate|ble_reprov|http_restart|mqtt_reboot))$/.test(sy.reset);
  const rssiTxt = typeof sy.rssi === 'number' ? ` · <span class="${sy.rssi <= -75 ? 'bad' : ''}" title="WiFi RSSI(dBm), -75 이하 = 약함">rssi ${sy.rssi}</span>` : '';
  // 접속 AP(0.3.6~): BSSID 뒤 3옥텟 + 채널. 같은 SSID 의 확장기/공유기 중 어디에 붙었는지.
  const apTxt = sy.bssid ? ` · <span title="접속 중인 AP BSSID ${esc(sy.bssid)} · 채널 ${sy.ch ?? '?'}. 같은 집 다른 기기와 다르면 확장기에 붙은 것">ap ${esc(sy.bssid.slice(-8))}${sy.ch ? ' ch' + sy.ch : ''}</span>` : '';
  // net 진단(0.3.6~): WiFi 링크 끊김(연결 상태에서 떨어진 것만) · MQTT 플래핑 · C6 코프로세서 링크.
  let netTxt = '';
  const nd = sy.net;
  if (nd) {
    const discAge = nd.disc_age_s ?? -1;
    const ageTxt = discAge < 0 ? '' : (discAge < 3600 ? Math.round(discAge/60) + 'm' : Math.round(discAge/3600) + 'h');
    const discBad = (nd.disc || 0) > 0 && discAge >= 0 && discAge < 3600;
    const discDetail = (nd.disc || 0) > 0 ? ` r${nd.reason} ${ageTxt}${nd.disc_bssid ? ' @' + esc(nd.disc_bssid.slice(-8)) : ''}` : '';
    netTxt += ` · <span class="${discBad ? 'bad' : ''}" title="WiFi 링크 끊김 횟수(부팅 후, 연결 상태에서 떨어진 것만) · 마지막 reason(200=beacon timeout, 201=AP 없음, 8=AP 가 끊음, 15=4-way timeout, 2/4=auth/assoc expire) · 경과 · 당시 AP. 1시간 내면 빨강. 끊길 때 AP 가 바뀌면 확장기 쪽">disc ${nd.disc || 0}${discDetail}</span>`;
    if (nd.flap) netTxt += ` · <span class="bad" title="MQTT 플래핑: 연결은 되는데 keepalive 응답이 없어 짧게 반복 끊김(10분 6회 이상). 10분 지속 시 다른 AP 로 이동, 20분이면 재부팅(reset=SW:mqtt_flap)">flap!</span>`;
    const cpSum = (nd.cp_fail || 0) + (nd.cp_down || 0) + (nd.cp_init || 0);
    netTxt += ` · <span class="${cpSum > 0 ? 'bad' : ''}" title="C6 코프로세서 링크: 전송 실패 / 전송 다운 / C6 재부팅 횟수(부팅 후). 0 이 아니면 WiFi 가 아니라 보드(SDIO/C6) 쪽">c6 ${nd.cp_fail || 0}/${nd.cp_down || 0}/${nd.cp_init || 0}</span>`;
  }
  return `<span class="${resetBad ? 'bad' : 'muted'}" title="마지막 재부팅 사유 · 가동시간 · 남은 힙">up ${up} · reset ${esc(sy.reset || '?')}${sy.heap ? ' · heap ' + Math.round(sy.heap/1024) + 'K' : ''}${typeof sy.int_free === 'number' ? ` · <span class="${sy.int_free < 80*1024 ? 'bad' : ''}" title="내부 RAM 여유/최대 블록(KB), 80K 미만 빨강">int ${Math.round(sy.int_free/1024)}K/${Math.round((sy.int_largest||0)/1024)}K</span>` : ''}${rssiTxt}${apTxt}${netTxt}</span>`;
}

// 카메라 행의 클립 파이프라인 요약 (clip_stats + image_state). 이상 신호가 있으면 빨강.
function clipCellHtml(r) {
  const c = r.clip_stats;
  if (!c) return '<small class="muted">—</small>';
  const bad = (c.skip > 0 || c.skip_lock > 0 || c.up_fail > 0 || c.sd_fail > 0 || (c.up_busy_s ?? -1) >= 300
    || (c.enc_err || 0) > 0 || (c.sd_fault || 0) >= 3 || !!(c.sys && c.sys.net && c.sys.net.flap));
  const age = c.last_rec_s >= 0 ? (c.last_rec_s < 3600 ? Math.round(c.last_rec_s/60)+'m' : Math.round(c.last_rec_s/3600)+'h') : '없음';
  const stuck = (c.up_busy_s ?? -1) >= 300 ? ` <span class="bad">up ${Math.round(c.up_busy_s/60)}m!</span>` : '';
  // SD 쓰기 fault 래치 누적(0.3.6~). 부팅마다 반복해서 오르면 카드 불량 → 교체.
  const sdf = typeof c.sd_fault === 'number' && c.sd_fault > 0 ? ` · <span class="${c.sd_fault >= 3 ? 'bad' : ''}" title="SD 쓰기 fault 래치 횟수(부팅 후). 3회 이상 = 카드 교체 검토">sdfault ${c.sd_fault}${c.sd_fault >= 3 ? ' 교체?' : ''}</span>` : '';
  const sy = c.sys;
  const syTxt = sy ? `<br>${sysStateText(sy)}` : '';
  // 인코더(0.2.1~): 누적 실패 / 현재 연속. 연속이 임계(50)에 닿으면 펌웨어가 enc_stall 로 재부팅. 0 이 아니면 빨강.
  const encTxt = typeof c.enc_err === 'number' && (c.enc_err > 0 || (c.enc_streak || 0) > 0)
    ? ` · <span class="bad" title="H.264 인코더 단계 실패 누적/현재 연속. 연속 50이면 enc_stall 자가 재부팅. 구 빌드(0.2.0 이하)는 1회 실패로 영구 사망·녹화 0">enc ${c.enc_err}/${c.enc_streak || 0}</span>` : '';
  const le = c.last_err;
  // esp_err_t 이름(0.3.6: 전송 중 끊김도 HTTP_CONNECT 로 통일 → "net"). 모르는 값은 숫자 그대로.
  const errName = e => ({ '-1': 'FAIL', '28675': 'net', '269': 'NOT_FINISHED(SD읽기)', '263': 'TIMEOUT', '257': 'NO_MEM' })[String(e)] || e;
  const leTxt = le ? `<br><span class="bad" title="마지막 업로드 실패: stage 1=upload-url 2=R2 PUT 3=meta 4=SD읽기 · esp_err(net=연결불가/전송중 끊김, 클립 시도횟수 미산입) · HTTP 상태(0=전송 실패) · 경과초">last_err stage${le.stage} err=${errName(le.err)} http${le.http} ${Math.round((le.age_s||0)/60)}m ago</span>` : '';
  const im = r.image_state;
  const imTxt = im ? `<br><span class="${im.ae_frozen ? 'bad' : 'muted'}" title="${esc(JSON.stringify(im))}">exp ${im.exp} · luma ${im.luma} · ${im.night ? 'NIGHT' : 'DAY'}${im.ae_frozen ? ' · AE동결' : ''}</span>` : '';
  return `<small class="${bad ? 'bad' : 'muted'}" title="${esc(JSON.stringify(c))}">rec ${c.rec} · skip ${(c.skip||0)+(c.skip_lock||0)} · fail ${(c.up_fail||0)+(c.sd_fail||0)} · sd ${c.sd_backlog||0} · ${age}${stuck}${encTxt}${sdf}${syTxt}${leTxt}${imTxt}</small>`;
}

// camera_logs 행 → 텍스트. 0.3.6~: "N (ms) …" 는 펌웨어 판단 기록(부팅 표식·재부팅 사유·AP 이동·
// 클립 유실·링 유실). 부팅 표식 앞엔 구분선을 넣어 타임라인을 부팅 단위로 읽게 한다(최신순이라 선은 아래쪽 경계).
function formatCameraLogs(rows) {
  if (!rows || !rows.length) return '(에러 로그 없음)';
  return rows.map(r =>
    (/^N \(\d+\) boot: /.test(r.msg) ? '──────── 부팅 ────────\n' : '') +
    fmtKstSec(r.created_at) + (r.prev_boot ? ' [이전 부팅]' : '') +
    ' up' + (r.uptime_s ?? '?') + 's ' + r.msg + (r.count > 1 ? '  x' + r.count : '')).join('\n');
}

// ─────────────────────────────────────────────────────────────────────────
// 베타 테스터 계정 — 계정 목록(localStorage 공유) + 배치 인증
// ─────────────────────────────────────────────────────────────────────────
// 운영 베타 10계정 (2026-10). beta.html 에서 편집하면 localStorage 'terra-beta-emails' 에 저장되고
// live.html 도 같은 목록을 쓴다.
const BETA_EMAILS_DEFAULT = [
  '01046590180@test.com', '01053911601@test.com', '01026448891@test.com', '01024896379@test.com',
  '01043576098@test.com', '01021120001@test.com', '01093115816@test.com', '01023636275@test.com',
  '01048533907@test.com', '01037689511@test.com',
];
function loadBetaEmails() {
  try {
    const v = JSON.parse(localStorage.getItem('terra-beta-emails') || 'null');
    if (Array.isArray(v) && v.length) return v;
  } catch (e) { /* 기본값 */ }
  return BETA_EMAILS_DEFAULT.slice();
}
function saveBetaEmails(list) {
  localStorage.setItem('terra-beta-emails', JSON.stringify(list));
}
// textarea(#reg-emails) → 이메일 배열 (빈 줄·공백 제거)
function parseEmailsInput() {
  const ta = document.getElementById('reg-emails');
  return ta ? ta.value.split('\n').map(e => e.trim()).filter(Boolean) : loadBetaEmails();
}

// 메인 세션(sb)은 안 건드리도록 persistSession:false 별도 클라이언트 사용.
// 배치 클라이언트의 signOut 은 전부 scope:'local' — 기본값 global 은 그 계정의 모든 기기 세션(테스터 폰 앱 포함)을
// 지워서 앱이 refresh_token_not_found 로 로그아웃된다 (2026-10-08 테스터 계정 일괄 조회 사고).
let sbBatch = null;
function getBatchClient() {
  if (!CONFIG.supabaseUrl) { toast('먼저 API Base 설정', 'bad'); return null; }
  if (!sbBatch) {
    sbBatch = window.supabase.createClient(CONFIG.supabaseUrl, CONFIG.supabasePublishable,
      { auth: { persistSession: false, autoRefreshToken: false, storageKey: 'terra-batch-reg' } });
  }
  return sbBatch;
}
// ⚠️ mosquitto 계정 정리·실제 명령 전달은 프로덕션 서버에서만 되므로 'API' 칸(#reg-apibase)이 있으면 그걸 우선.
function regApiBase() {
  const el = document.getElementById('reg-apibase');
  return (el && el.value.trim().replace(/\/$/, '')) || CONFIG.apiBase;
}
function regPassword() {
  const el = document.getElementById('reg-pass');
  return el ? el.value : '123456';
}

// 계정별 access_token 캐시 — 백엔드가 JWKS 로 로컬 검증하므로 signOut 뒤에도 만료(1h) 전엔 유효.
// 로그인 rate limit(약 30회/5분)을 아끼려고 조회 때 받은 토큰을 재사용한다.
const REG_TOKENS = {};   // email → { token, uid, exp(초) }
function cacheRegToken(email, session) {
  if (session && session.access_token) {
    REG_TOKENS[email] = { token: session.access_token, uid: session.user && session.user.id, exp: session.expires_at || 0 };
  }
}
async function regAccessToken(email) {
  return (await regSession(email)).token;
}
// {token, uid} — 캐시 유효하면 로그인 없이 반환. uid 는 commands INSERT(issued_by) 에 필요.
async function regSession(email) {
  const c = REG_TOKENS[email];
  if (c && c.exp - Date.now() / 1000 > 60) return c;
  const client = getBatchClient(); if (!client) throw new Error('설정 없음');
  const { data, error } = await client.auth.signInWithPassword({ email, password: regPassword() });
  if (error) throw new Error(error.message);
  cacheRegToken(email, data.session);
  await client.auth.signOut({ scope: 'local' });
  return REG_TOKENS[email];
}
// 그 계정 JWT 로 API 호출. 응답 {ok, status, body}. 상태코드 분기가 필요한 곳(라이브 409/429)용.
async function regFetch(email, path, opts = {}) {
  const token = await regAccessToken(email);
  const res = await fetch(regApiBase() + path, {
    cache: 'no-store', ...opts,
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token, ...(opts.headers || {}) },
  });
  const text = await res.text();
  let body = text; try { body = JSON.parse(text); } catch (e) { /* 텍스트 그대로 */ }
  log({ [opts.method || 'GET']: path, account: email, status: res.status, body });
  return { ok: res.ok, status: res.status, body, headers: res.headers };
}

// ─────────────────────────────────────────────────────────────────────────
// OTA 모달 (Stage J) — 릴리스 드롭다운 → prepare 발행 → 진행 상태 자동 갱신 → ready 면 apply 버튼
// 한 버튼으로 2단계를 다룬다: 진행 중 작업이 없으면 릴리스를 골라 prepare, ready 면 apply 를 묻는다.
// kind: 'cameras' | 'devices', target: 'camera_p4' | 'device_nano' | 'hub_p4'
// 페이지에 #ota-modal / #ota-label / #ota-body 가 있어야 한다.
// ─────────────────────────────────────────────────────────────────────────
// 허브(cameras.model == esp32-p4-hub)는 카메라 행이지만 펌웨어 프로젝트가 달라 hub_p4 릴리스만 받는다(서버 target_for 와 동일 규칙).
function otaTargetOf(isCam, row) {
  if (!isCam) return 'device_nano';
  return row && row.model === 'esp32-p4-hub' ? 'hub_p4' : 'camera_p4';
}

// auth: 베타 계정에서 열 때 {token, base} — 그 계정 JWT 로 호출(소유자 아니면 404). 없으면 메인 세션 api()(index.html).
const OTA = { kind: null, id: null, label: '', target: null, timer: null, busy: false, auth: null };

async function otaApi(path, opts = {}) {
  if (!OTA.auth) {
    if (typeof api !== 'function') { toast('로그인 세션 없음', 'bad'); return null; }
    return api(path, opts);
  }
  try {
    const res = await fetch(OTA.auth.base + path, {
      cache: 'no-store', ...opts,
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + OTA.auth.token, ...(opts.headers || {}) },
    });
    const text = await res.text();
    let body; try { body = JSON.parse(text); } catch (e) { body = text; }
    if (!res.ok) {
      toast(`${res.status}: ${typeof body === 'object' ? (body.detail || JSON.stringify(body)) : body}`, 'bad');
      return null;
    }
    return body;
  } catch (e) {
    toast('네트워크 에러: ' + e.message, 'bad');
    return null;
  }
}
const OTA_ACTIVE = ['pending', 'accepted', 'downloading', 'ready', 'applying'];
const OTA_STATUS_KO = {
  pending: '명령 발행됨 · 기기 응답 대기', accepted: '기기가 수락 · 재부팅 후 다운로드 시작', downloading: '다운로드 중',
  ready: '다운로드·검증 완료 · 적용 대기', applying: '전환 + 재부팅 중 · 새 펌웨어 확정 대기',
  verified: '적용 완료 (새 버전 확정)', rolled_back: '롤백됨 (이전 버전으로 복귀)', failed: '실패', timeout: '시간 초과',
};
const OTA_GATE_KO = {
  offline: '오프라인', never_seen: 'heartbeat 없음', no_ota_capability: '구 펌웨어(OTA 미지원)', weak_wifi: 'WiFi 약함(< -75dBm)',
  // uploading/live_active 는 2026-10-16 폐지(펌웨어 0.4.5 가 거절 대신 보류) — 옛 작업 기록 표시용으로만 남긴다.
  low_internal_ram: '내부 RAM 부족', just_booted: '부팅 직후(크래시 후 5분·정상 1분)',
  uploading: '클립 업로드 중(폐지)', live_active: '라이브 시청 중(폐지)',
};

function otaEntity(kind, id, label, target, auth) {
  Object.assign(OTA, { kind, id, label, target, busy: false, auth: auth || null });
  document.getElementById('ota-label').innerText = label;
  document.getElementById('ota-body').innerHTML = '불러오는 중…';
  document.getElementById('ota-modal').classList.remove('hidden');
  otaRefresh();
  clearInterval(OTA.timer);
  OTA.timer = setInterval(otaRefresh, 5000);
}

function closeOta() {
  clearInterval(OTA.timer);
  OTA.timer = null;
  OTA.id = null;
  document.getElementById('ota-modal').classList.add('hidden');
}

function otaAgo(iso) {
  if (!iso) return '';
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 60 ? `${s}초 전` : s < 3600 ? `${Math.floor(s / 60)}분 전` : `${Math.floor(s / 3600)}시간 전`;
}

function otaJobLine(j) {
  const cls = j.status === 'verified' ? 'good' : ['failed', 'rolled_back', 'timeout'].includes(j.status) ? 'bad' : '';
  const extra = [j.error ? `error=${j.error}` : '', j.forced ? '게이트 무시' : '', j.prev_version ? `← ${j.prev_version}` : ''].filter(Boolean).join(' · ');
  return `<div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
    <span class="status ${cls}" style="min-width:90px">${esc(j.status)}</span>
    <code>${esc(j.version || '?')}</code>
    <span class="muted">${OTA_STATUS_KO[j.status] || ''}${j.status === 'downloading' ? ` ${j.pct || 0}%` : ''}</span>
    <span class="muted" style="margin-left:auto">${otaAgo(j.updated_at)}${extra ? ' · ' + esc(extra) : ''}</span>
  </div>`;
}

async function otaRefresh() {
  if (!OTA.id || OTA.busy) return;
  const [jobs, rels] = await Promise.all([
    otaApi('/firmware/jobs?target_uuid=' + OTA.id + '&limit=5'),
    otaApi('/firmware/releases?target=' + OTA.target),
  ]);
  if (!OTA.id) return;  // 기다리는 사이 닫힘
  const active = (jobs || []).find(j => OTA_ACTIVE.includes(j.status));
  const history = (jobs || []).filter(j => j !== active);
  let html = '';

  if (active && active.status === 'ready') {
    html += `<div style="margin-bottom:10px">${otaJobLine(active)}</div>
      <button onclick="otaApply('${active.id}')" style="font-size:14px;padding:6px 14px">2단계 적용 — ${esc(active.version)} 로 전환 + 재부팅</button>
      <div class="muted" style="margin-top:6px">녹화·라이브가 잠깐 끊겨도 되는 시점에 눌러. 전환 뒤 새 펌웨어가 MQTT 로 붙으면 <b>verified</b>, 못 붙으면 5분 뒤 자동 롤백.</div>`;
  } else if (active) {
    const pct = active.status === 'downloading' ? (active.pct || 0) : active.status === 'pending' || active.status === 'accepted' ? 0 : 100;
    html += `<div style="margin-bottom:6px">${otaJobLine(active)}</div>
      <div style="background:#2a2a2a;border-radius:4px;height:8px;overflow:hidden"><div style="width:${pct}%;height:100%;background:#4a8;transition:width .5s"></div></div>
      <div class="muted" style="margin-top:6px">진행 중 — 5초마다 자동 갱신. ${active.status === 'applying' ? '기기가 재부팅 중이라 30초~1분 뒤 verified/rolled_back 으로 끝남.' : '끝나면 적용 버튼이 나타남.'}</div>`;
  } else if (!rels || !rels.length) {
    html += `<div class="muted">등록된 릴리스 없음 — <code>scripts/upload_firmware.py</code> 로 먼저 등록.</div>`;
  } else {
    const opts = rels.map(r => `<option value="${esc(r.id)}">${esc(r.version)} — ${(r.size_bytes / 1048576).toFixed(2)}MB${r.notes ? ' · ' + esc(r.notes) : ''}</option>`).join('');
    html += `<label style="display:block;margin-bottom:6px">릴리스
        <select id="ota-release" style="width:100%;margin-top:4px;font-size:13px">${opts}</select></label>
      <label style="display:block;margin-bottom:10px"><input type="checkbox" id="ota-force"> 사전 점검 무시(force) — 오프라인·약한 WiFi·내부 RAM 부족·부팅 직후도 강행 (개발용)<br>
      <small class="muted">녹화·업로드·라이브 중은 더 이상 막지 않는다 — 펌웨어 0.4.5+ 가 한가해질 때까지 최대 240초 미룬 뒤 재부팅한다.</small></label>
      <button onclick="otaPrepare()" style="font-size:14px;padding:6px 14px">1단계 다운로드 (prepare)</button>
      <div class="muted" style="margin-top:6px">기기가 한 번 재부팅해 받아 두기만 한다. 부팅 파티션은 안 바뀌어서 지금 버전으로 계속 동작.</div>`;
  }
  if (history.length) {
    html += `<details style="margin-top:12px"><summary class="muted">최근 작업 ${history.length}건</summary>
      <div style="display:flex;flex-direction:column;gap:4px;margin-top:6px">${history.map(otaJobLine).join('')}</div></details>`;
  }
  document.getElementById('ota-body').innerHTML = html;
}

async function otaPrepare() {
  const sel = document.getElementById('ota-release');
  const force = document.getElementById('ota-force').checked;
  if (!sel || !sel.value) return;
  const version = sel.options[sel.selectedIndex].text.split(' — ')[0];
  if (force && !confirm(`${OTA.label}: ${version} 사전 점검을 무시하고 보낼까?`)) return;
  OTA.busy = true;
  try {
    const r = await otaApi(`/${OTA.kind}/${OTA.id}/ota`, { method: 'POST', body: JSON.stringify({ release_id: sel.value, force }) });
    log({ ota_prepare: r });
    if (!r) return;
    const ok = r.published !== false && r.status !== 'failed';
    if (ok) {
      toast(`prepare 발행 · ${r.version}${r.gate_reasons && r.gate_reasons.length ? ' (게이트 무시: ' + r.gate_reasons.map(g => OTA_GATE_KO[g] || g).join(', ') + ')' : ''}`, 'good');
    } else {
      toast(`prepare 실패: ${r.detail || r.status}`, 'bad');
    }
  } finally {
    OTA.busy = false;
    otaRefresh();
  }
}

async function otaApply(jobId) {
  if (!confirm(`${OTA.label}: 지금 전환 + 재부팅할까? (약 30초 다운타임)`)) return;
  OTA.busy = true;
  try {
    const r = await otaApi(`/${OTA.kind}/${OTA.id}/ota/${jobId}/apply`, { method: 'POST' });
    log({ ota_apply: r });
    if (r) toast(r.published === false ? 'apply 발행 실패(브로커) — 잠시 후 재시도' : `apply 발행 · ${r.version}`, r.published === false ? 'bad' : 'good');
  } finally {
    OTA.busy = false;
    otaRefresh();
  }
}
