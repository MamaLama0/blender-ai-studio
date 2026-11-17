import { Viewer } from "./viewer.js";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

let backend = null;
let state = null;
let imagePath = "";
// Si WebGL est indisponible, le reste de l'interface doit continuer de marcher.
let viewer;
try {
  viewer = new Viewer($("#viewport"));
} catch (err) {
  console.error(err);
  viewer = { load: async () => { throw new Error("WebGL indisponible"); }, play() {}, togglePause() { return true; }, actions: {} };
  $(".stage-empty h1").textContent = "Aperçu 3D indisponible";
  $(".stage-empty p").textContent = "WebGL ne démarre pas sur cette machine. Le pipeline fonctionne quand même : ouvre le résultat dans Blender. Essaie aussi de lancer l'app avec BAS_SOFTWARE_GL=1.";
}

// Appelle un slot Python et renvoie une promesse
const call = (name, ...args) => new Promise((resolve) => backend[name](...args, resolve));

// ------------------------------------------------------------------ connexion à Python
new QWebChannel(qt.webChannelTransport, async (channel) => {
  backend = channel.objects.backend;
  window.studio = { backend }; // pratique pour déboguer depuis la console
  backend.event.connect((raw) => handle(JSON.parse(raw)));
  applyState(JSON.parse(await call("getState")));
  setInterval(() => backend.refresh(), 8000); // suit la connexion de Blender
});

function handle(ev) {
  switch (ev.kind) {
    case "state": applyState(ev.state); break;
    case "log": addLog(ev.text, ev.level); break;
    case "toast": toast(ev.text, ev.level); break;
    case "busy": setBusy(ev.on, ev.label); break;
    case "preview": if (ev.url) loadPreview(ev.url); break;
    case "agent": agentEvent(ev); break;
    case "confirm": showConfirm(ev); break;
    case "exported": addLog(`Fichier exporté : ${ev.path}`); break;
  }
}

// ------------------------------------------------------------------ état général
let lastPreview = null;

function applyState(s) {
  state = s;
  const p = s.project;
  $("#projectName").textContent = p.name || "Aucun projet";

  const gpu = $("#gpuStatus");
  gpu.textContent = s.gpu.label;
  gpu.title = s.gpu.advice;
  $("#gpuAdvice").textContent = s.gpu.advice;

  const live = $("#blenderStatus");
  live.classList.toggle("live", s.blender_live);
  live.lastElementChild.textContent = s.blender_live ? "Blender connecté" : "Blender fermé";
  $("#openBlenderBtn").hidden = s.blender_live;

  const st = s.settings;
  $("#providerLabel").textContent = st.llm_provider === "claude"
    ? modelName(st.claude_model)
    : `Ollama, modèle ${st.ollama_model}`;
  $("#engineHint").textContent = engineText(st.generator);
  $("#facesRange").value = st.target_faces;
  $("#facesOut").textContent = fmt(st.target_faces);

  const info = p.info || {};
  markDone("create", !!info.mesh);
  markDone("clean", !!info.mesh);
  markDone("rig", !!info.rig);
  markDone("animate", (info.animations || []).length > 0);
  markDone("export", !!info.last_export);
  if (info.mesh) {
    $("#meshStat").innerHTML = `<b>${fmt(info.mesh.faces)}</b> faces, <b>${fmt(info.mesh.vertices)}</b> sommets, hauteur 1 m.`;
  }
  $("#rigStat").innerHTML = info.rig
    ? `<b>${info.rig.bones.length}</b> os, poids ${info.rig.skinning}${info.rig.has_legs ? "" : ", pas de jambes détectées"}.`
    : "";

  if (p.preview && p.preview.split("?")[0] !== (lastPreview || "").split("?")[0]) loadPreview(p.preview);
  else if (!p.preview) $("#stage").classList.remove("has-model");
  setBusy(s.busy);
}

function modelName(id) {
  // claude-sonnet-5-5 → Claude Sonnet 5.5
  const [, family, major, minor] = id.match(/^claude-([a-z]+)-(\d+)-(\d+)/) || [];
  return family ? `Claude ${family[0].toUpperCase()}${family.slice(1)} ${major}.${minor}` : id;
}

function engineText(gen) {
  return {
    trellis: "Moteur : TRELLIS sur ta carte graphique.",
    hunyuan_local: "Moteur : serveur Hunyuan3D local.",
    custom_command: "Moteur : ta commande personnalisée.",
    import_only: "Pas de moteur de génération : importe un fichier 3D.",
  }[gen] || "";
}

const fmt = (n) => Number(n || 0).toLocaleString("fr-FR");

function markDone(step, done) {
  $(`.step[data-step="${step}"]`).classList.toggle("done", done);
}

// ------------------------------------------------------------------ aperçu 3D
async function loadPreview(url) {
  lastPreview = url;
  try {
    const clips = await viewer.load(url);
    $("#stage").classList.add("has-model");
    renderClips(clips);
  } catch (err) {
    addLog(`Aperçu impossible : ${err.message || err}`, "error");
  }
}

const CLIP_LABELS = { Idle: "Respiration", Walk: "Marche", Jump: "Saut", Wave: "Coucou" };

function renderClips(names) {
  const box = $("#clipList");
  box.innerHTML = "";
  $("#clips").hidden = names.length === 0;
  const order = Object.keys(CLIP_LABELS);
  const rank = (n) => (order.includes(n) ? order.indexOf(n) : 99);
  for (const name of [...names].sort((a, b) => rank(a) - rank(b))) {
    const b = document.createElement("button");
    b.textContent = CLIP_LABELS[name] || name;
    b.setAttribute("aria-pressed", String(viewer.current === viewer.actions[name]));
    b.onclick = () => {
      viewer.play(name);
      $$("#clipList button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      setPlayIcon(false);
    };
    box.append(b);
  }
}

function setPlayIcon(paused) {
  $("#playBtn use").setAttribute("href", paused ? "#i-play" : "#i-pause");
  $("#playBtn").setAttribute("aria-label", paused ? "Lecture" : "Pause");
}
$("#playBtn").onclick = () => setPlayIcon(viewer.togglePause());

// ------------------------------------------------------------------ étapes
$$(".step-head").forEach((head) => {
  head.onclick = () => openStep(head.closest(".step").dataset.step);
});

function openStep(name) {
  $$(".step").forEach((s) => $(".step-head", s).setAttribute("aria-expanded", String(s.dataset.step === name)));
}

// 1. Créer
const drop = $("#imageDrop");
drop.onclick = async (e) => {
  e.preventDefault();
  const path = await call("pickFile", "image");
  if (!path) return;
  const copy = JSON.parse(await call("adoptImage", path));
  setImage(copy.path, copy.url);
};
drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") drop.click(); };

function setImage(path, src) {
  imagePath = path;
  const img = $("#imageThumb");
  img.src = src;
  img.hidden = false;
  drop.classList.add("has-image");
}

$("#generateBtn").onclick = () => {
  const text = $("#promptText").value.trim();
  if (!imagePath && !text) return toast("Ajoute une image ou une description d'abord.", "warn");
  backend.generate(JSON.stringify({ image: imagePath, text, name: text.split(" ").slice(0, 3).join("-") }));
};

$("#importBtn").onclick = async () => {
  const path = await call("pickFile", "model");
  if (path) backend.importModel(path);
};

// 2. Alléger
$("#facesRange").oninput = (e) => ($("#facesOut").textContent = fmt(e.target.value));
$("#reimportBtn").onclick = async () => {
  const source = state?.project?.info?.source;
  if (!source) return toast("Il n'y a pas encore de modèle à ré-importer.", "warn");
  await call("saveSettings", JSON.stringify({ target_faces: Number($("#facesRange").value) }));
  backend.importModel(source);
};

// 3. Rig
$("#rigBtn").onclick = () => backend.rig();

// 4. Animer
$("#intensityRange").oninput = (e) => ($("#intensityOut").textContent = `${e.target.value} %`);
$("#animateBtn").onclick = () => {
  const kinds = $$("#animChoice input:checked").map((i) => i.value);
  if (!kinds.length) return toast("Choisis au moins une animation.", "warn");
  backend.animate(JSON.stringify({ kinds, intensity: Number($("#intensityRange").value) / 100 }));
};

// 5. Exporter
$$(".export").forEach((b) => (b.onclick = () => backend.exportModel(b.dataset.fmt)));
$("#folderBtn").onclick = () => backend.openFolder();

// Haut de page
$("#openBlenderBtn").onclick = () => backend.openBlender();
$("#projectPill").onclick = () => {
  const name = prompt("Nom du nouveau projet", "perso");
  if (name !== null) { lastPreview = null; viewer._framed = false; backend.newProject(name); }
};

// ------------------------------------------------------------------ glisser-déposer
const stage = $("#stage");
let dragDepth = 0;
for (const target of [stage, drop]) {
  target.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; if (target === stage) $("#dropVeil").hidden = false; else drop.classList.add("over"); });
  target.addEventListener("dragover", (e) => e.preventDefault());
  target.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#dropVeil").hidden = true; drop.classList.remove("over"); } });
  target.addEventListener("drop", async (e) => {
    e.preventDefault();
    dragDepth = 0;
    $("#dropVeil").hidden = true;
    drop.classList.remove("over");
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const dataUrl = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(file); });
    const saved = await call("saveDropped", file.name, dataUrl);
    if (/\.(png|jpe?g|webp)$/i.test(file.name)) { setImage(saved, dataUrl); openStep("create"); }
    else backend.importModel(saved);
  });
}

// ------------------------------------------------------------------ assistant
const messages = $("#messages");

function addMsg(cls, text) {
  $("#chatEmpty")?.remove();
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.textContent = text;
  messages.append(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}

const TOOL_LABELS = {
  get_scene_info: "Regarde la scène",
  get_object_info: "Inspecte un objet",
  run_pipeline_step: "Lance une étape",
  execute_blender_code: "Exécute du code",
  get_viewport_screenshot: "Capture la vue 3D",
};
let lastTool = null;

function agentEvent(ev) {
  if (ev.kind === "assistant") addMsg("assistant", ev.text);
  else if (ev.kind === "guard") addMsg("guard", ev.text);
  else if (ev.kind === "tool") {
    lastTool = addMsg("tool", "");
    const detail = ev.input?.explanation || (ev.input?.step ? `étape ${ev.input.step}` : ev.input?.name || "");
    lastTool.innerHTML = "";
    const d = document.createElement("details");
    const s = document.createElement("summary");
    s.textContent = `${TOOL_LABELS[ev.name] || ev.name}${detail ? " : " + detail : ""}`;
    d.append(s);
    lastTool.append(d);
  } else if (ev.kind === "tool_result" && lastTool) {
    lastTool.classList.add(ev.error ? "err" : "ok");
    const pre = document.createElement("pre");
    pre.textContent = ev.text;
    $("details", lastTool).append(pre);
    if (ev.error) $("details", lastTool).open = true;
  }
}

function showConfirm(ev) {
  $("#chatEmpty")?.remove();
  const node = $("#confirmTpl").content.firstElementChild.cloneNode(true);
  $(".confirm-why", node).textContent = ev.explanation;
  $("code", node).textContent = ev.code;
  for (const b of $$("button", node)) {
    b.onclick = () => {
      const yes = b.dataset.answer === "yes";
      backend.confirmReply(ev.id, yes);
      node.classList.add("answered");
      $(".confirm-title", node).textContent = yes ? "Code exécuté dans Blender" : "Code refusé";
    };
  }
  messages.append(node);
  messages.scrollTop = messages.scrollHeight;
  $("button[data-answer=yes]", node).focus();
}

$("#composer").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#chatInput").value.trim();
  if (!text) return;
  addMsg("user", text);
  $("#chatInput").value = "";
  backend.chat(text);
};
$("#chatInput").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
};
$$(".suggest").forEach((b) => (b.onclick = () => { $("#chatInput").value = b.textContent; $("#chatInput").focus(); }));
$("#stopBtn").onclick = () => backend.stopAgent();
$("#resetChatBtn").onclick = () => {
  messages.innerHTML = "";
  backend.resetChat();
};

// ------------------------------------------------------------------ occupé / journal / toasts
function setBusy(on, label) {
  $("#progress").hidden = !on;
  $("#stopBtn").hidden = !on;
  $$("#generateBtn, #importBtn, #reimportBtn, #rigBtn, #animateBtn, .export, #sendBtn").forEach((b) => (b.disabled = !!on));
  if (on && label) addLog(`${label}…`);
}

function addLog(text, level = "info") {
  const time = new Date().toLocaleTimeString("fr-FR");
  $("#journalLast").textContent = text.split("\n")[0];
  const li = document.createElement("li");
  li.className = level;
  li.textContent = `${time}  ${text}`;
  $("#journalList").append(li);
  $("#journalList").scrollTop = 1e9;
}
$("#journalToggle").onclick = () => {
  const open = $("#journal").classList.toggle("open");
  $("#journalToggle").setAttribute("aria-expanded", String(open));
};

function toast(text, level = "info") {
  const t = document.createElement("div");
  t.className = `toast ${level}`;
  t.textContent = text;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), level === "error" ? 9000 : 4500);
}

// ------------------------------------------------------------------ réglages
const dlg = $("#settingsDialog");
const form = $("#settingsForm");

$("#settingsBtn").onclick = () => {
  const st = state.settings;
  for (const el of form.elements) {
    if (!el.name || el.name === "api_key") continue;
    if (el.type === "radio") el.checked = st[el.name] === el.value;
    else if (el.type === "checkbox") el.checked = !!st[el.name];
    else if (el.name in st) el.value = st[el.name];
  }
  form.elements.api_key.value = "";
  $("#keyState").textContent = state.has_api_key ? "(déjà enregistrée)" : "(manquante)";
  syncWhen();
  dlg.showModal();
};

function syncWhen() {
  const data = new FormData(form);
  $$(".when", form).forEach((el) => {
    const [k, v] = el.dataset.when.split("=");
    el.classList.toggle("on", data.get(k) === v);
  });
}
form.addEventListener("change", syncWhen);

form.addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "save") return;
  e.preventDefault();
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "radio") { if (el.checked) out[el.name] = el.value; }
    else if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "number") out[el.name] = Number(el.value);
    else out[el.name] = el.value;
  }
  toast(await call("saveSettings", JSON.stringify(out)));
  dlg.close();
});
