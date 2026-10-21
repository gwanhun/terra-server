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

// ─────────────────────────────────────────────────────────────────────────
// 상태 텍스트 색 기준 (2026-10-21 재정리)
//   bad(빨강)  = 지금 조치: 크래시/전원 부족 재부팅이 24시간 안, 클립 유실(SD 재업로드까지 실패), 업로드 고착(≥300초),
//                인코더 연속 실패, MQTT 플래핑, SD fault 3회+, 내부 RAM 여유 32K 미만 또는 최대 블록 16K 미만, RSSI -80 이하
//   warn(주황) = 관찰: 펌웨어가 스스로 복구한 재부팅(net_wd·*_stall 등, 24시간 안), 약한 WiFi(-75~-80), 스킵·즉시 업로드 실패
//                (SD 로 재시도됨), 인코더 누적 실패(지금은 복구), SD 백로그, WiFi 끊김 1시간 안, C6 링크 카운터
//   (회색)     = 정상·의도된 재부팅(전원 투입·원격 재부팅·OTA·회전·재설정). 하루 넘게 잘 돌면 재부팅 사유 색은 뺀다.
// 전엔 "whitelist 밖 reset 이면 줄 전체 빨강"이라 OTA 적용 재부팅 뒤 65시간 잘 도는 허브도 빨갛게 보였다.
// 내부 RAM(2026-10-21 2차 보정): 옛 기준 80K 미만은 P4 운영 평시(여유 60~128K, 최대 블록 31~42K — specs/stage-j-ota.md B1)가
//   거의 다 걸려 노이즈였다. 16KB 미만 할당은 내부 RAM 에서만 나오므로(SPIRAM_MALLOC_ALWAYSINTERNAL=16384) 진짜 위험선은
//   최대 블록 16K 미만(할당 실패 시작) / 여유 32K 미만. 주황은 최대 블록 24K·여유 48K 미만. OTA 게이트는 최대 블록 8K.
// ─────────────────────────────────────────────────────────────────────────
const RESET_EXPECTED = /^(POWERON|USB|DEEPSLEEP|SW|SW:(mqtt_reboot|ota_apply|ota_prepare|rotate|ble_reprov|http_restart))(\(.*\))?$/;
const RESET_CRASH = /^(PANIC|WDT|INT_WDT|TASK_WDT|BROWNOUT)/;
const RESET_KO = {
  POWERON: '전원 투입', USB: 'USB 리플래시 직후', DEEPSLEEP: '딥슬립 복귀', SW: '펌웨어 재부팅(사유 기록 없음)',
  'SW:mqtt_reboot': '앱/서버 원격 재부팅', 'SW:ota_apply': 'OTA 적용(슬롯 전환)', 'SW:ota_prepare': 'OTA 다운로드 전 재부팅',
  'SW:rotate': '회전 설정 적용', 'SW:ble_reprov': 'BLE 재설정', 'SW:http_restart': 'HTTP 재시작 요청',
  'SW:net_wd': '네트워크 워치독 — WiFi/MQTT 오래 끊겨 자가 재부팅', 'SW:boot_net_wd': '부팅 중 네트워크 워치독',
  'SW:mqtt_stuck': 'MQTT 먹통 자가 재부팅', 'SW:mqtt_flap': 'MQTT 플래핑 20분 지속 자가 재부팅',
  'SW:rtc_loop_stall': 'WebRTC 루프 스톨 자가 재부팅', 'SW:rtc_send_stall': 'WebRTC 전송 스톨', 'SW:rtc_lock_stall': 'WebRTC 락 스톨',
  'SW:cam_stall': '카메라 무프레임 자가 재부팅', 'SW:enc_stall': 'H.264 인코더 연속 실패 자가 재부팅', 'SW:upload_stuck': '업로드 고착 자가 재부팅',
  PANIC: '크래시(패닉)', INT_WDT: '인터럽트 워치독 크래시', TASK_WDT: '태스크 워치독 크래시', WDT: '워치독 크래시',
  BROWNOUT: '전원 부족 — 펌프 기동 시 전압 강하 등, 어댑터·케이블 점검',
};
// 재부팅 사유 등급. uptime 이 24시간 넘으면 ''(오래된 일).
function resetLevel(reset, uptimeS) {
  if (!reset || RESET_EXPECTED.test(reset)) return '';
  if (typeof uptimeS === 'number' && uptimeS >= 24 * 3600) return '';
  return RESET_CRASH.test(reset) ? 'bad' : 'warn';
}
function resetTitle(reset) {
  const key = String(reset || '').replace(/\(.*\)$/, '');
  const ko = RESET_KO[key] || (key.startsWith('SW:boot:') ? '부팅 단계 워치독 (' + key.slice(8) + ')' : '알 수 없는 사유');
  return `마지막 재부팅 사유: ${reset} — ${ko}. 24시간 넘게 잘 돌면 색 없음`;
}
const rssiLevel = r => (typeof r !== 'number' ? '' : r <= -80 ? 'bad' : r <= -75 ? 'warn' : '');

// sys_state / clip_stats.sys → 한 줄 텍스트 (가동시간·리셋 사유·힙·RSSI·AP·net 진단). 토큰별로만 색을 입힌다.
function sysStateText(sy) {
  const up = sy.uptime_s >= 0 ? (sy.uptime_s < 3600 ? Math.round(sy.uptime_s/60)+'m' : (sy.uptime_s/3600).toFixed(1)+'h') : '?';
  const rl = resetLevel(sy.reset, sy.uptime_s);
  const resetTxt = `<span class="${rl}" title="${esc(resetTitle(sy.reset))}">reset ${esc(sy.reset || '?')}</span>`;
  const rssiTxt = typeof sy.rssi === 'number' ? ` · <span class="${rssiLevel(sy.rssi)}" title="WiFi RSSI(dBm). -75 이하 약함(주황), -80 이하 매우 약함(빨강)">rssi ${sy.rssi}</span>` : '';
  const intLv = typeof sy.int_free !== 'number' ? '' : (sy.int_free < 32*1024 || (sy.int_largest || 1e9) < 16*1024) ? 'bad' : (sy.int_free < 48*1024 || (sy.int_largest || 1e9) < 24*1024) ? 'warn' : '';
  const intTxt = typeof sy.int_free === 'number' ? ` · <span class="${intLv}" title="내부 RAM 여유/최대 블록(KB). P4 평시 60~128K/31~42K. 16KB 미만 할당은 내부 RAM 에서만 나와서 최대 블록 16K 미만(또는 여유 32K 미만)이면 할당 실패 시작 → 빨강. 24K/48K 미만 주황">int ${Math.round(sy.int_free/1024)}K/${Math.round((sy.int_largest||0)/1024)}K</span>` : '';
  // 접속 AP(0.3.6~): BSSID 뒤 3옥텟 + 채널. 같은 SSID 의 확장기/공유기 중 어디에 붙었는지.
  const apTxt = sy.bssid ? ` · <span title="접속 중인 AP BSSID ${esc(sy.bssid)} · 채널 ${sy.ch ?? '?'}. 같은 집 다른 기기와 다르면 확장기에 붙은 것">ap ${esc(sy.bssid.slice(-8))}${sy.ch ? ' ch' + sy.ch : ''}</span>` : '';
  // net 진단(0.3.6~): WiFi 링크 끊김(연결 상태에서 떨어진 것만) · MQTT 플래핑 · C6 코프로세서 링크.
  let netTxt = '';
  const nd = sy.net;
  if (nd) {
    const discAge = nd.disc_age_s ?? -1;
    const ageTxt = discAge < 0 ? '' : (discAge < 3600 ? Math.round(discAge/60) + 'm' : Math.round(discAge/3600) + 'h');
    const discRecent = (nd.disc || 0) > 0 && discAge >= 0 && discAge < 3600;
    const discDetail = (nd.disc || 0) > 0 ? ` r${nd.reason} ${ageTxt}${nd.disc_bssid ? ' @' + esc(nd.disc_bssid.slice(-8)) : ''}` : '';
    netTxt += ` · <span class="${discRecent ? 'warn' : ''}" title="WiFi 링크 끊김 횟수(부팅 후) · 마지막 reason(200=beacon timeout, 201=AP 없음, 8=AP 가 끊음, 15=4-way timeout, 2/4=auth/assoc expire) · 경과 · 당시 AP. 1시간 안이면 주황(관찰). 끊길 때 AP 가 바뀌면 확장기 쪽">disc ${nd.disc || 0}${discDetail}</span>`;
    if (nd.flap) netTxt += ` · <span class="bad" title="MQTT 플래핑: 연결은 되는데 keepalive 응답이 없어 짧게 반복 끊김(10분 6회 이상). 10분 지속 시 다른 AP 로 이동, 20분이면 재부팅(reset=SW:mqtt_flap)">flap!</span>`;
    const cpSum = (nd.cp_fail || 0) + (nd.cp_down || 0) + (nd.cp_init || 0);
    if (cpSum > 0) netTxt += ` · <span class="warn" title="C6 코프로세서 링크: 전송 실패 / 전송 다운 / C6 재부팅 횟수(부팅 후). 0 이 아니면 WiFi 가 아니라 보드(SDIO/C6) 쪽. 계속 오르면 보드 점검">c6 ${nd.cp_fail || 0}/${nd.cp_down || 0}/${nd.cp_init || 0}</span>`;
  }
  return `<span class="muted" title="가동시간 · 마지막 재부팅 사유 · 남은 힙">up ${up} · ${resetTxt}${sy.heap ? ' · heap ' + Math.round(sy.heap/1024) + 'K' : ''}${intTxt}${rssiTxt}${apTxt}${netTxt}</span>`;
}

// 카메라 행의 클립 파이프라인 요약 (clip_stats + image_state). 카운터는 부팅 후 누적이라 "지금 위험"과 "예전에 한 번"을 구분한다.
//   bad : 클립 유실(sd_fail, 마지막 실패가 24시간 안) · 업로드 고착(up_busy_s ≥300) · 인코더 연속 실패 · SD fault 3회+ · MQTT 플래핑 · SD 백로그 10+ · 스킵이 녹화의 20% 이상(3건+)
//   warn: 스킵·즉시 업로드 실패(SD 로 재시도되므로) · 유실이 있었지만 24시간 넘게 전 · 인코더 누적 실패(연속 0 = 복구됨) · SD fault 2 · SD 백로그 3~9 · 1시간 안 마지막 실패
//   회색: SD 백로그 1~2(부팅 직후 업로드 대기는 정상) · SD fault 1 · 24시간 넘은 실패
// 카운터는 부팅 후 누적이라 시점을 모른다 — last_err.age_s 로 "최근인지"를 가늠한다(없으면 최근으로 본다).
function clipCellHtml(r) {
  const c = r.clip_stats;
  if (!c) return '<small class="muted">—</small>';
  const skips = (c.skip || 0) + (c.skip_lock || 0), upFail = c.up_fail || 0, lost = c.sd_fail || 0, backlog = c.sd_backlog || 0;
  const stuck = (c.up_busy_s ?? -1) >= 300;
  const encErr = c.enc_err || 0, encStreak = c.enc_streak || 0, sdFault = c.sd_fault || 0;
  const flap = !!(c.sys && c.sys.net && c.sys.net.flap);
  const skipBad = skips >= 3 && skips >= (c.rec || 0) * 0.2;
  const le = c.last_err;
  const errAge = le && typeof le.age_s === 'number' ? le.age_s : -1;    // 마지막 업로드 실패 경과초(-1 = 기록 없음)
  const failRecent = errAge < 0 || errAge < 24 * 3600;                    // 24시간 넘은 실패는 지금 문제 아님
  const lostLv = lost > 0 ? (failRecent ? 'bad' : 'warn') : upFail > 0 ? (failRecent ? 'warn' : '') : '';
  const backlogLv = backlog >= 10 ? 'bad' : backlog >= 3 ? 'warn' : '';
  const sdFaultLv = sdFault >= 3 ? 'bad' : sdFault >= 2 ? 'warn' : '';
  const level = (lostLv === 'bad' || stuck || encStreak > 0 || sdFaultLv === 'bad' || flap || backlogLv === 'bad' || skipBad) ? 'bad'
    : (skips > 0 || lostLv === 'warn' || encErr > 0 || sdFaultLv === 'warn' || backlogLv === 'warn') ? 'warn' : '';
  const age = c.last_rec_s >= 0 ? (c.last_rec_s < 3600 ? Math.round(c.last_rec_s/60)+'m' : Math.round(c.last_rec_s/3600)+'h') : '없음';
  const tok = (cls, title, txt) => `<span class="${cls}" title="${esc(title)}">${txt}</span>`;
  const parts = [
    tok('', '녹화 완료 건수(부팅 후)', `rec ${c.rec}`),
    tok(skipBad ? 'bad' : skips ? 'warn' : '', '스킵 = 녹화 못 한 모션(슬롯 없음 + H.264 락 실패). 녹화의 20% 이상이면 빨강', `skip ${skips}`),
    tok(lostLv, `즉시 업로드 실패 ${upFail}(SD 로 재시도됨) / SD 재업로드 실패 ${lost}(클립 유실). 마지막 실패가 24시간 안이면 색, 지났으면 회색` + (errAge >= 0 ? ` · 마지막 실패 ${Math.round(errAge/3600)}시간 전` : ''), `fail ${upFail + lost}`),
    tok(backlogLv, 'SD 에 쌓인 업로드 대기 건수. 1~2 는 정상(부팅 직후·연속 모션), 3+ 주황, 10+ 빨강(R2 접속 불가 의심)', `sd ${backlog}`),
    tok('', '마지막 녹화 후 경과(모션이 없으면 길어지는 게 정상)', age),
  ];
  if (stuck) parts.push(tok('bad', '한 클립 업로드가 5분 넘게 진행 중 — 고착', `up ${Math.round(c.up_busy_s/60)}m!`));
  // 인코더(0.2.1~): 누적 실패 / 현재 연속. 연속이 임계(50)에 닿으면 펌웨어가 enc_stall 로 재부팅.
  if (encErr > 0 || encStreak > 0) parts.push(tok(encStreak > 0 ? 'bad' : 'warn', 'H.264 인코더 실패 누적/현재 연속. 연속 0 이면 복구된 것(주황), 연속 중이면 빨강(50이면 enc_stall 재부팅)', `enc ${encErr}/${encStreak}`));
  // SD 쓰기 fault 래치 누적(0.3.6~). 부팅마다 반복해서 오르면 카드 불량 → 교체.
  if (sdFault > 0) parts.push(tok(sdFaultLv, 'SD 쓰기 fault 래치 횟수(부팅 후). 1 회색, 2 주황, 3회 이상 빨강 = 카드 교체 검토', `sdfault ${sdFault}${sdFault >= 3 ? ' 교체?' : ''}`));
  const sy = c.sys;
  const syTxt = sy ? `<br>${sysStateText(sy)}` : '';
  // esp_err_t 이름(0.3.6: 전송 중 끊김도 HTTP_CONNECT 로 통일 → "net"). 모르는 값은 숫자 그대로.
  const errName = e => ({ '-1': 'FAIL', '28675': 'net', '269': 'NOT_FINISHED(SD읽기)', '263': 'TIMEOUT', '257': 'NO_MEM' })[String(e)] || e;
  const leRecent = le && (le.age_s || 0) < 3600;
  const leTxt = le ? `<br>${tok(leRecent ? 'warn' : '', '마지막 업로드 실패: stage 1=upload-url 2=R2 PUT 3=meta 4=SD읽기 · esp_err(net=연결불가/전송중 끊김) · HTTP 상태(0=전송 실패) · 경과. 1시간 안이면 주황, 지났으면 회색(이미 재시도됨)', `last_err stage${le.stage} err=${errName(le.err)} http${le.http} ${Math.round((le.age_s||0)/60)}m ago`)}` : '';
  const im = r.image_state;
  const imTxt = im ? `<br>${tok(im.ae_frozen ? 'warn' : '', JSON.stringify(im) + (im.ae_frozen ? ' · AE 진동 감지로 노출 고정됨(영상은 나옴)' : ''), `exp ${im.exp} · luma ${im.luma} · ${im.night ? 'NIGHT' : 'DAY'}${im.ae_frozen ? ' · AE동결' : ''}`)}` : '';
  const dot = level ? `<span class="${level}" title="${level === 'bad' ? '지금 조치 필요' : '관찰'}">● </span>` : '';
  return `<small class="muted" title="${esc(JSON.stringify(c))}">${dot}${parts.join(' · ')}${syTxt}${leTxt}${imTxt}</small>`;
}

// 모바일 카드형 표 — thead 의 th 텍스트를 각 td 의 data-label 로 복사해 ≤600px 에서 "라벨 값" 줄로 세운다(shared.css .table-wrap).
// 표는 .table-wrap 으로 감싸 데스크톱에선 가로 스크롤(패널 밖으로 안 삐져나오게). 행이 Realtime 으로 다시 그려져도
// 따라가게 tbody 변경을 MutationObserver 로 본다(childList 만 보므로 data-label setAttribute 가 재귀를 일으키지 않는다).
function enableTableLabels(root = document) {
  const stamp = (table) => {
    const ths = [...table.querySelectorAll('thead th')].map(th => th.textContent.trim());
    table.querySelectorAll('tbody tr').forEach(tr => {
      let col = 0;
      [...tr.children].forEach(td => {
        if (!td.hasAttribute('data-label')) td.setAttribute('data-label', td.colSpan > 1 ? '' : (ths[col] || ''));
        col += td.colSpan || 1;
      });
    });
  };
  root.querySelectorAll('table').forEach(table => {
    if (!table.parentElement.classList.contains('table-wrap')) {
      const w = document.createElement('div'); w.className = 'table-wrap';
      table.parentNode.insertBefore(w, table); w.appendChild(table);
    }
    stamp(table);
    new MutationObserver(() => stamp(table)).observe(table, { childList: true, subtree: true });
  });
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
// 테스터 이름(2026-10-21 owner 전달). 계정 textarea 에 "이메일 이름" 으로 적으면 localStorage 'terra-beta-names' 가 우선.
const BETA_NAMES_DEFAULT = {
  '01046590180@test.com': '김태훈', '01053911601@test.com': '손휘윤', '01026448891@test.com': '우지인', '01024896379@test.com': '김창열',
  '01043576098@test.com': '김대중', '01021120001@test.com': '원인식', '01093115816@test.com': '임영아', '01023636275@test.com': '조세영',
  '01048533907@test.com': '전영준', '01037689511@test.com': '김희원',
};
function loadBetaNames() {
  try {
    const v = JSON.parse(localStorage.getItem('terra-beta-names') || 'null');
    if (v && typeof v === 'object') return { ...BETA_NAMES_DEFAULT, ...v };
  } catch (e) { /* 기본값 */ }
  return { ...BETA_NAMES_DEFAULT };
}
function testerName(email) { return loadBetaNames()[email] || ''; }
// 전화번호 계정(01046590180@test.com) → "4659-0180"
function phoneOf(email) {
  const m = /^(\d{3})(\d{4})(\d{4})@/.exec(email);
  return m ? `${m[2]}-${m[3]}` : '';
}
// "김태훈 · 4659-0180" (이름 없으면 번호만, 번호 형식 아니면 이메일 앞부분)
function testerLabel(email) {
  const n = testerName(email), p = phoneOf(email);
  return [n, p].filter(Boolean).join(' · ') || email.split('@')[0];
}
// textarea 기본 내용 — "이메일 이름" 한 줄씩
function betaEmailsText() {
  const names = loadBetaNames();
  return loadBetaEmails().map(e => names[e] ? `${e} ${names[e]}` : e).join('\n');
}
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
// textarea(#reg-emails) → 이메일 배열. 한 줄 "이메일 [이름]" — 이름이 있으면 'terra-beta-names' 에 저장.
function parseEmailsInput() {
  const ta = document.getElementById('reg-emails');
  if (!ta) return loadBetaEmails();
  const names = loadBetaNames(); let changed = false;
  const emails = ta.value.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
    const [email, ...rest] = l.split(/\s+/);
    const name = rest.join(' ').trim();
    if (name && names[email] !== name) { names[email] = name; changed = true; }
    return email;
  });
  if (changed) localStorage.setItem('terra-beta-names', JSON.stringify(names));
  return emails;
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
