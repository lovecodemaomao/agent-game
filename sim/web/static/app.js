/* 对战回放前端：Canvas 地图 + 回合步进 + 资源/动作/日志面板 */
const CELL = 16;
const COLORS = {
  challenger: "#66d9a5", defender: "#ff8f8f", robot: "#c9a0ff",
  station: "#ffd479", wall: "#8fa8c7", gatling: "#7ec8ff",
  railgun: "#d0a2ff", rocket: "#ffb26b",
};
let data = null;       // {rounds: [...]}
let cur = 0;
let timer = null;

const $ = (id) => document.getElementById(id);

async function loadList() {
  const r = await fetch("/api/matches");
  const j = await r.json();
  const sel = $("matchSel");
  sel.innerHTML = "";
  for (const m of j.matches) {
    const o = document.createElement("option");
    o.value = m; o.textContent = m;
    sel.appendChild(o);
  }
}

async function loadMatch() {
  const name = $("matchSel").value;
  if (!name) return;
  const r = await fetch("/api/match?name=" + encodeURIComponent(name));
  const j = await r.json();
  data = j.rounds;
  cur = 0;
  render();
}

function roundObj() { return data ? data[cur] : null; }

/* ---------------- 地图渲染 ---------------- */
function drawMap() {
  const rd = roundObj();
  const ctx = $("cv").getContext("2d");
  ctx.fillStyle = "#0a0e14";
  ctx.fillRect(0, 0, $("cv").width, $("cv").height);
  if (!rd) return;
  const view = $("viewSel").value;
  const req = rd.teams[view] && rd.teams[view].request;
  if (!req) return;

  // 矿区/中立
  for (const z of req.mapInfo.zones) {
    const [x, y] = [z.pos.x, z.pos.y];
    let c = null, label = null;
    if (z.neutralType === "stone") { c = "#5a6b7d"; label = "石"; }
    else if (z.neutralType === "iron") { c = "#7d6b5a"; label = "铁"; }
    else if (z.neutralType === "copper") { c = "#6b7d5a"; label = "铜"; }
    else if (z.neutralType === "vendor") { c = "#ffd479"; label = "贩"; }
    else if (z.neutralType === "weaponShop") { c = "#7ec8ff"; label = "商"; }
    else if (z.neutralType.includes("TaskPoint")) { c = "#3a4a63"; label = "任"; }
    if (c) {
      ctx.fillStyle = c;
      ctx.fillRect(x * CELL + 2, y * CELL + 2, CELL - 4, CELL - 4);
      ctx.fillStyle = "#0a0e14"; ctx.font = "9px monospace"; ctx.textAlign = "center";
      ctx.fillText(label, x * CELL + CELL / 2, y * CELL + CELL / 2 + 3);
    }
  }
  // 单位
  const drawUnit = (u, team) => {
    const color = COLORS[team] || "#fff";
    const size = u.roleType === "station" ? CELL * 2 : CELL - 2;
    ctx.fillStyle = color;
    ctx.fillRect(u.pos.x * CELL + 1, u.pos.y * CELL + 1, size - 2, size - 2);
    ctx.fillStyle = "#0a0e14"; ctx.font = "8px monospace"; ctx.textAlign = "center";
    const tag = { worker: "工", pioneer: "开", station: "基", wall: "墙",
                  gatling: "加", railgun: "狙", rocket: "火" }[u.roleType] || "?";
    ctx.fillText(tag, u.pos.x * CELL + size / 2, u.pos.y * CELL + size / 2 + 3);
    if (u.roleType !== "wall" && u.health !== undefined) {
      // 血条
      const maxhp = { worker: 220, pioneer: 200, station: 4500, gatling: 2000,
                      railgun: 2000, rocket: 2000, wall: 2000 }[u.roleType] || 100;
      const frac = Math.max(0, u.health / maxhp);
      ctx.fillStyle = "#333";
      ctx.fillRect(u.pos.x * CELL + 1, u.pos.y * CELL - 2, size - 2, 2);
      ctx.fillStyle = frac > 0.5 ? "#66d9a5" : frac > 0.25 ? "#ffd479" : "#ff6b6b";
      ctx.fillRect(u.pos.x * CELL + 1, u.pos.y * CELL - 2, (size - 2) * frac, 2);
    }
  };
  for (const u of req.teamOur.roles) drawUnit(u, view);
  for (const u of (req.teamEnemy.roles || [])) drawUnit(u, view === "challenger" ? "defender" : "challenger");
  for (const u of (req.robot.roles || [])) {
    drawUnit({ ...u, roleType: "robot" }, "robot");
    ctx.fillStyle = "#fff"; ctx.font = "8px monospace"; ctx.textAlign = "center";
    ctx.fillText(u.roleType[0].toUpperCase(), u.pos.x * CELL + CELL / 2, u.pos.y * CELL + CELL / 2 + 3);
  }
  // 动作目标标记
  const markTarget = (cmd) => {
    if (!cmd.targetPos) return;
    for (const p of cmd.targetPos) {
      ctx.strokeStyle = "#ffd479"; ctx.lineWidth = 1;
      ctx.strokeRect(p.x * CELL + 0.5, p.y * CELL + 0.5, CELL - 1, CELL - 1);
    }
  };
  for (const team of ["challenger", "defender"]) {
    const t = rd.teams[team];
    if (!t) continue;
    const map = t.response.roleCommandMap || {};
    for (const k in map) markTarget(map[k]);
  }
}

/* ---------------- 面板 ---------------- */
function renderPanels() {
  const rd = roundObj();
  if (!rd) return;
  $("roundInfo").textContent = `回合 ${rd.roundNo} / ${data.length}（第${Math.ceil(rd.roundNo / 130)}天）`;

  // 资源
  const tbl = $("resTable");
  const ch = rd.teams.challenger.request.teamOur;
  const de = rd.teams.defender.request.teamOur;
  const rows = [
    ["金币", ch.goldNum, de.goldNum],
    ["总积分", ch.totalScore, de.totalScore],
    ["单位数", ch.roles.length, de.roles.length],
    ["武器数", ch.roles.filter(r => ["gatling","railgun","rocket"].includes(r.roleType)).length,
              de.roles.filter(r => ["gatling","railgun","rocket"].includes(r.roleType)).length],
    ["围墙数", ch.roles.filter(r => r.roleType === "wall").length,
              de.roles.filter(r => r.roleType === "wall").length],
  ];
  tbl.innerHTML = "<tr><th></th><th class='ch'>挑战者</th><th class='de'>防守者</th></tr>" +
    rows.map(r => `<tr><td>${r[0]}</td><td class='ch'>${r[1]}</td><td class='de'>${r[2]}</td></tr>`).join("");

  // 动作
  const act = $("actions");
  let html = "";
  for (const team of ["challenger", "defender"]) {
    const t = rd.teams[team];
    if (!t) continue;
    const own = {};
    for (const u of t.request.teamOur.roles) own[u.id] = u;
    const map = t.response.roleCommandMap || {};
    const names = { move: "移动", attack: "攻击", sell: "贩卖", buy: "购买", build: "建造",
                    remove: "拆除", acceptTask: "接任务", submitAnswer: "提交答案",
                    summonTreasure: "召唤宝藏", use: "使用", drop: "丢弃", collect: "采集" };
    for (const k in map) {
      const c = map[k];
      const u = own[k] || { roleType: "?" };
      const tgt = c.targetPos ? ` → (${c.targetPos.map(p => p.x + "," + p.y).join(")(")})` :
                  c.name ? ` ${c.name}` : "";
      html += `<div><span class="${team === "challenger" ? "ch" : "de"}">${u.roleType}#${k}</span> ` +
              `${names[c.action] || c.action}${tgt}</div>`;
    }
    if (t.abnormal) html += `<div class="bad">⚠ ${team} 异常: ${t.abnormal}</div>`;
    if (t.dropped && t.dropped.length) html += `<div class="bad">${t.dropped.join("<br>")}</div>`;
  }
  act.innerHTML = html || "（本回合双方无动作）";

  // 事件日志
  const filter = $("logFilter").value;
  const log = $("log");
  const evs = (rd.events || []).filter(e => !filter || e.type === filter);
  log.innerHTML = evs.map(e => `<div class="ev-${e.type}">[${e.type}] ${JSON.stringify(
    {...e, type: undefined, round: undefined})}</div>`).join("") || "（无）";
}

/* ---------------- 积分曲线 ---------------- */
function drawScore() {
  const cv = $("cvScore"), ctx = cv.getContext("2d");
  ctx.fillStyle = "#10141c"; ctx.fillRect(0, 0, cv.width, cv.height);
  if (!data || !data.length) return;
  const chS = data.map(r => r.teams.challenger.request.teamOur.totalScore);
  const deS = data.map(r => r.teams.defender.request.teamOur.totalScore);
  const maxS = Math.max(1, ...chS, ...deS);
  const plot = (arr, color) => {
    ctx.strokeStyle = color; ctx.beginPath();
    arr.forEach((s, i) => {
      const x = (i / (arr.length - 1 || 1)) * cv.width;
      const y = cv.height - (s / maxS) * (cv.height - 10) - 5;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  };
  plot(chS, "#66d9a5");
  plot(deS, "#ff8f8f");
  // 当前位置竖线
  const x = (cur / (data.length - 1 || 1)) * cv.width;
  ctx.strokeStyle = "#ffd479";
  ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, cv.height); ctx.stroke();
}

function render() { drawMap(); renderPanels(); drawScore(); }

/* ---------------- 控制 ---------------- */
$("btnLoad").onclick = loadMatch;
$("btnPrev").onclick = () => { if (cur > 0) { cur--; render(); } };
$("btnNext").onclick = () => { if (data && cur < data.length - 1) { cur++; render(); } };
$("viewSel").onchange = render;
$("logFilter").onchange = renderPanels;
$("btnPlay").onclick = () => {
  if (timer) { clearInterval(timer); timer = null; $("btnPlay").textContent = "▶ 播放"; return; }
  $("btnPlay").textContent = "⏸ 暂停";
  timer = setInterval(() => {
    if (!data || cur >= data.length - 1) { clearInterval(timer); timer = null; $("btnPlay").textContent = "▶ 播放"; return; }
    cur++; render();
  }, parseInt($("speed").value));
};
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") $("btnPrev").onclick();
  if (e.key === "ArrowRight") $("btnNext").onclick();
});

loadList();
