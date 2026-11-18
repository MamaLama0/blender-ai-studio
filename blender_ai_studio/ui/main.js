import { Viewer } from "./viewer.js";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const fmt = (n) => Number(n || 0).toLocaleString("fr-FR");
// Typographie française : espaces insécables autour de « », avant : ; ? !
const fr = (t) => String(t)
  .replace(/« /g, "«\u00a0").replace(/ »/g, "\u00a0»")
  .replace(/ ([:;?!])(?=\s|$)/g, "\u00a0$1");

let backend = null;
let state = null;
let imagePath = "";
let prefs = {};

// Si WebGL est indisponible, le reste de l'interface doit continuer de marcher.
let viewer;
try {
  viewer = new Viewer($("#viewport"));
} catch (err) {
  console.error(err);
  const noop = () => {};
  viewer = {
    load: async () => { throw new Error("WebGL indisponible"); }, play: noop, togglePause: () => true,
    clear: noop, frame: noop, setWireframe: noop, setSkeleton: () => true, setTurntable: noop,
    setSpeed: noop, capture: () => null, actions: {}, options: {},
  };
  $(".stage-empty h1").textContent = "Aperçu 3D indisponible";
  $(".stage-empty p").textContent = "WebGL ne démarre pas sur cette machine. Le pipeline fonctionne quand même : ouvre le résultat dans Blender. Essaie aussi de lancer l'app avec BAS_SOFTWARE_GL=1.";
}

// Appelle un slot Python et renvoie une promesse
const call = (name, ...args) => new Promise((resolve) => backend[name](...args, resolve));

// ------------------------------------------------------------------ connexion à Python
new QWebChannel(qt.webChannelTransport, async (channel) => {
  backend = channel.objects.backend;
  window.studio = { backend, viewer }; // pratique pour déboguer depuis la console
  backend.event.connect((raw) => handle(JSON.parse(raw)));
  prefs = JSON.parse(await call("getUiPrefs"));
  applyPrefs();
  applyState(JSON.parse(await call("getState")));
  await loadChatHistory();
  setInterval(() => backend.refresh(), 8000); // suit la connexion de Blender
});

function handle(ev) {
  switch (ev.kind) {
    case "state": applyState(ev.state); break;
    case "log": addLog(ev.text, ev.level); break;
    case "toast": toast(ev.text, ev.level, ev.action); break;
    case "busy": setBusy(ev.on, ev.label, ev.started); break;
    case "preview": if (ev.url) loadPreview(ev.url); break;
    case "agent": agentEvent(ev); break;
    case "confirm": showConfirm(ev); break;
    case "exported": addLog(`Fichier exporté : ${ev.path}`); break;
    case "project_changed": resetWorkspaceView(); break;
    case "step_done": onStepDone(ev.step); break;
    case "test_result": showTestResult(ev); break;
  }
}

// ------------------------------------------------------------------ préférences d'interface
// Rangées côté Python : le port du serveur local change à chaque lancement,
// donc le localStorage du navigateur serait perdu.
function savePrefs(patch) {
  Object.assign(prefs, patch);
  backend?.setUiPrefs(JSON.stringify(prefs));
}

function applyPrefs() {
  if ("autoChain" in prefs) $("#autoChain").checked = prefs.autoChain;
  if (prefs.kinds) $$("#animChoice input").forEach((i) => (i.checked = prefs.kinds.includes(i.value)));
  if (prefs.intensity) {
    $("#intensityRange").value = prefs.intensity;
    $("#intensityOut").textContent = `${prefs.intensity} %`;
  }
  for (const tool of ["skeleton", "wireframe", "turntable"]) {
    if (prefs[tool]) setTool(tool, true, { quiet: true });
  }
  if (prefs.speed) setSpeed(prefs.speed);
}

// ------------------------------------------------------------------ état général
let lastPreview = null;
let focusNextStep = true;

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
  live.title = s.blender_live ? "Blender est ouvert et piloté par l'app" : "Ouvrir Blender avec le pont";
  $("#openBlenderBtn").hidden = s.blender_live;

  const st = s.settings;
  $("#providerLabel").textContent = st.llm_provider === "claude"
    ? modelName(st.claude_model)
    : `Ollama, modèle ${st.ollama_model}`;
  $("#engineHint").textContent = engineText(st.generator);
  $("#generateBtn").hidden = st.generator === "import_only";
  $("#facesRange").value = st.target_faces;
  $("#facesOut").textContent = fmt(st.target_faces);

  const info = p.info || {};
  const anims = info.animations || [];
  markStep("create", !!info.mesh, info.mesh ? `${fmt(info.mesh.faces)} faces` : "");
  markStep("clean", !!info.mesh, info.mesh ? "Nettoyé" : "");
  markStep("rig", !!info.rig, info.rig ? `${info.rig.bones.length} os` : "");
  markStep("animate", anims.length > 0, anims.length ? `${anims.length} animation${anims.length > 1 ? "s" : ""}` : "");
  markStep("export", !!info.last_export, info.last_export ? info.last_export.split(".").pop().toUpperCase() : "");
  $("#meshStat").innerHTML = info.mesh
    ? `<b>${fmt(info.mesh.faces)}</b> faces, <b>${fmt(info.mesh.vertices)}</b> sommets, hauteur 1 m.`
    : "Le modèle est nettoyé à l'import : sommets en double, faces cassées, normales, mise à l'échelle.";
  $("#rigStat").innerHTML = info.rig
    ? `<b>${info.rig.bones.length}</b> os, poids ${info.rig.skinning}${info.rig.has_legs ? "" : ", pas de jambes détectées"}.`
    : "";
  // Une étape n'a de sens que si la précédente est faite
  $("#reimportBtn").disabled = !info.source;
  $("#rigBtn").disabled = !info.mesh;
  $("#animateBtn").disabled = !info.rig;

  if (focusNextStep) {
    // À l'ouverture d'un projet : on montre la première étape qui reste à faire.
    focusNextStep = false;
    const todo = [["create", info.mesh], ["rig", info.rig], ["animate", anims.length], ["export", info.last_export]]
      .find(([, done]) => !done);
    openStep(todo ? todo[0] : "export");
  }

  renderChecklist(s.checks);
  $("#composerHint").textContent = s.blender_live ? "" : "Blender fermé : l'IA ne peut pas agir.";

  if (p.preview && p.preview.split("?")[0] !== (lastPreview || "").split("?")[0]) loadPreview(p.preview);
  else if (!p.preview) setHasModel(false);
  if (!s.busy) setBusy(false);
}

function modelName(id) {
  // claude-sonnet-5-5 → Claude Sonnet 5.5
  const [, family, major, minor] = id.match(/^claude-([a-z]+)-(\d+)-(\d+)/) || [];
  return family ? `Claude ${family[0].toUpperCase()}${family.slice(1)} ${major}.${minor}` : id;
}

function engineText(gen) {
  return {
    trellis: "Moteur : TRELLIS sur ta carte graphique.",
    hunyuan_local: "Moteur : serveur Hunyuan3D local (à partir d'une image).",
    custom_command: "Moteur : ta commande personnalisée.",
    import_only: "Pas de moteur de génération : importe un fichier 3D, ou choisis un moteur dans Réglages.",
  }[gen] || "";
}

function markStep(step, done, summary) {
  const el = $(`.step[data-step="${step}"]`);
  el.classList.toggle("done", done);
  $(".step-state", el).textContent = summary;
}

const STEP_ORDER = ["create", "clean", "rig", "animate", "export"];
function onStepDone(step) {
  // Après une étape, on ouvre la suivante : le prochain geste est sous la main.
  const next = { import: "rig", rig: "animate", animate: "export" }[step];
  if (next) openStep(next);
}

// ------------------------------------------------------------------ check-list de démarrage
function renderChecklist(checks) {
  const ul = $("#checklist");
  ul.innerHTML = "";
  if (!checks) return;
  const rows = [
    ["blender", "Blender", "settings"],
    ["ai", "Assistant", "settings"],
    ["generator", "Génération 3D", "settings"],
  ];
  const allOk = rows.every(([k]) => checks[k].ok);
  ul.hidden = allOk;
  for (const [key, title] of rows) {
    const c = checks[key];
    const li = document.createElement("li");
    li.className = c.ok ? "ok" : "todo";
    li.innerHTML = `<span class="tick" aria-hidden="true"></span><b></b><span class="c-label"></span>`;
    $("b", li).textContent = title;
    $(".c-label", li).textContent = c.label;
    if (!c.ok) {
      const btn = document.createElement("button");
      btn.className = "btn small ghost";
      btn.textContent = "Régler";
      btn.onclick = () => openSettings();
      li.append(btn);
    }
    ul.append(li);
  }
}

// ------------------------------------------------------------------ aperçu 3D
function setHasModel(on) {
  $("#stage").classList.toggle("has-model", on);
  $("#tools").hidden = !on;
  if (!on) $("#clips").hidden = true;
}

async function loadPreview(url) {
  lastPreview = url;
  try {
    const clips = await viewer.load(url);
    setHasModel(true);
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
  setPlayIcon(!!viewer.paused);
}

function setPlayIcon(paused) {
  $("#playBtn use").setAttribute("href", paused ? "#i-play" : "#i-pause");
  $("#playBtn").setAttribute("aria-label", paused ? "Lecture" : "Pause");
}
$("#playBtn").onclick = () => setPlayIcon(viewer.togglePause());

const SPEEDS = [0.5, 1, 2];
function setSpeed(x) {
  viewer.setSpeed(x);
  $("#speedBtn").textContent = `${String(x).replace(".", ",")}×`;
}
$("#speedBtn").onclick = () => {
  const now = viewer.speed || 1;
  const next = SPEEDS[(SPEEDS.indexOf(now) + 1) % SPEEDS.length];
  setSpeed(next);
  savePrefs({ speed: next });
};

function setTool(tool, on, { quiet = false } = {}) {
  const btn = $(`#tools [data-tool="${tool}"]`);
  if (tool === "skeleton") {
    const ok = viewer.setSkeleton(on);
    if (on && !ok && !quiet) toast("Pas encore de squelette : passe l'étape 3 d'abord.", "warn");
  } else if (tool === "wireframe") viewer.setWireframe(on);
  else if (tool === "turntable") viewer.setTurntable(on);
  btn?.setAttribute("aria-pressed", String(on));
  if (!quiet) savePrefs({ [tool]: on });
}

async function capture() {
  if (!$("#stage").classList.contains("has-model")) return;
  const url = viewer.capture();
  if (url) await call("saveCapture", url);
}

$("#tools").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tool]");
  if (!btn) return;
  const tool = btn.dataset.tool;
  if (tool === "frame") viewer.frame();
  else if (tool === "capture") capture();
  else setTool(tool, btn.getAttribute("aria-pressed") !== "true");
});

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
  if (e.target.closest("#imageClear")) return;
  const path = await call("pickFile", "image");
  if (!path) return;
  const copy = JSON.parse(await call("adoptImage", path));
  setImage(copy.path, copy.url);
};
drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); drop.click(); } };

function setImage(path, src) {
  imagePath = path;
  const img = $("#imageThumb");
  img.src = src;
  img.hidden = false;
  drop.classList.add("has-image");
  $("#imageClear").hidden = false;
}

function clearImage() {
  imagePath = "";
  $("#imageThumb").hidden = true;
  $("#imageThumb").removeAttribute("src");
  drop.classList.remove("has-image");
  $("#imageClear").hidden = true;
}
$("#imageClear").onclick = (e) => { e.stopPropagation(); clearImage(); };

function autoOptions() {
  if (!$("#autoChain").checked) return null;
  return { kinds: selectedKinds(), intensity: Number($("#intensityRange").value) / 100 };
}
$("#autoChain").onchange = (e) => savePrefs({ autoChain: e.target.checked });

function generate() {
  const text = $("#promptText").value.trim();
  if (!imagePath && !text) return toast("Ajoute une image ou une description d'abord.", "warn");
  backend.generate(JSON.stringify({
    image: imagePath, text, name: text.split(/\s+/).slice(0, 3).join(" "), auto: autoOptions(),
  }));
}
$("#generateBtn").onclick = generate;
$("#promptText").onkeydown = (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); generate(); }
};

async function importFile() {
  const path = await call("pickFile", "model");
  if (!path) return;
  importPath(path);
}
function importPath(path) {
  const auto = autoOptions();
  if (auto) backend.importModelAuto(path, JSON.stringify(auto));
  else backend.importModel(path);
}
$("#importBtn").onclick = importFile;

// 2. Alléger
$("#facesRange").oninput = (e) => ($("#facesOut").textContent = fmt(e.target.value));
$("#reimportBtn").onclick = async () => {
  const source = state?.project?.info?.source;
  if (!source) return toast("Il n'y a pas encore de modèle à ré-importer.", "warn");
  await call("saveSettings", JSON.stringify({ target_faces: Number($("#facesRange").value) }));
  importPath(source); // refait aussi squelette, animations et export si l'enchaînement est coché
};

// 3. Squelette
$("#rigBtn").onclick = () => backend.rig();

// 4. Animer
const selectedKinds = () => $$("#animChoice input:checked").map((i) => i.value);
$("#animChoice").onchange = () => savePrefs({ kinds: selectedKinds() });
$("#intensityRange").oninput = (e) => ($("#intensityOut").textContent = `${e.target.value} %`);
$("#intensityRange").onchange = (e) => savePrefs({ intensity: Number(e.target.value) });
$("#animateBtn").onclick = () => {
  const kinds = selectedKinds();
  if (!kinds.length) return toast("Choisis au moins une animation.", "warn");
  backend.animate(JSON.stringify({ kinds, intensity: Number($("#intensityRange").value) / 100 }));
};

// 5. Exporter
$$(".export").forEach((b) => (b.onclick = () => backend.exportModel(b.dataset.fmt)));
$("#folderBtn").onclick = () => backend.openFolder();

// Haut de page
$("#openBlenderBtn").onclick = () => backend.openBlender();
$("#blenderStatus").onclick = () => { if (!state?.blender_live) backend.openBlender(); };
$("#cancelBtn").onclick = () => backend.cancel();
$("#helpBtn").onclick = () => $("#keysDialog").showModal();

// ------------------------------------------------------------------ projets
const projDlg = $("#projectsDialog");

async function openProjects() {
  const list = JSON.parse(await call("listProjects"));
  const ul = $("#projectList");
  ul.innerHTML = "";
  if (!list.length) ul.innerHTML = '<li class="project-empty">Aucun projet pour l\'instant.</li>';
  for (const p of list) ul.append(projectRow(p));
  $("#newProjectForm").elements.name.value = "";
  if (!projDlg.open) projDlg.showModal();
  $("#newProjectForm").elements.name.focus();
}

function projectRow(p) {
  const li = document.createElement("li");
  li.className = "project-row";
  const open = document.createElement("button");
  open.type = "button";
  open.className = "p-open";
  open.setAttribute("aria-current", String(p.current));
  const date = new Date(p.modified * 1000).toLocaleString("fr-FR", { dateStyle: "medium", timeStyle: "short" });
  open.innerHTML = `<span class="p-name"></span><span class="p-date">${date}</span>
    <span class="p-steps" title="${p.steps_done} étape(s) sur 4">${[0, 1, 2, 3].map((i) => `<i class="${i < p.steps_done ? "on" : ""}"></i>`).join("")}</span>`;
  $(".p-name", open).textContent = p.name;
  open.onclick = () => { projDlg.close(); if (!p.current) backend.openProject(p.folder); };

  const rename = iconButton("i-edit", "Renommer");
  rename.onclick = () => {
    const input = document.createElement("input");
    input.value = p.name;
    input.className = "p-rename";
    input.setAttribute("aria-label", "Nouveau nom");
    open.replaceWith(input);
    input.focus();
    input.select();
    let finished = false;
    const finish = async (commit) => {
      if (finished) return;
      finished = true;
      if (commit && input.value.trim() && input.value.trim() !== p.name) {
        const err = await call("renameProject", p.folder, input.value.trim());
        if (err) toast(err, "error");
      }
      openProjects();
    };
    input.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    };
    input.onblur = () => finish(true);
  };

  const del = iconButton("i-erase", "Mettre à la corbeille");
  del.onclick = async () => {
    if (del.dataset.armed !== "1") {
      // Deuxième clic pour confirmer : pas de fenêtre de plus, mais pas d'accident non plus.
      del.dataset.armed = "1";
      del.classList.add("armed");
      del.title = "Clique encore pour confirmer";
      setTimeout(() => { del.dataset.armed = ""; del.classList.remove("armed"); del.title = "Mettre à la corbeille"; }, 3000);
      return;
    }
    const err = await call("deleteProject", p.folder);
    if (err) toast(err, "error");
    else toast(`« ${p.name} » est à la corbeille.`);
    openProjects();
  };
  li.append(open, rename, del);
  return li;
}

function iconButton(icon, label) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "icon-btn";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.innerHTML = `<svg viewBox="0 0 24 24"><use href="#${icon}"/></svg>`;
  return b;
}

$("#projectPill").onclick = openProjects;
$("#newProjectForm").addEventListener("submit", (e) => {
  if (e.submitter?.value !== "create" && e.submitter) return;
  backend.newProject($("#newProjectForm").elements.name.value.trim());
});

function resetWorkspaceView() {
  lastPreview = null;
  viewer.clear?.();
  setHasModel(false);
  clearImage();
  $("#promptText").value = "";
  clearChat();
  loadChatHistory();
  focusNextStep = true;
}

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
    if (!/\.(png|jpe?g|webp|glb|gltf|obj|fbx|stl|ply)$/i.test(file.name)) {
      return toast("Format non pris en charge : image (png, jpg, webp) ou modèle 3D (glb, obj, fbx, stl, ply).", "warn");
    }
    const dataUrl = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(file); });
    const saved = await call("saveDropped", file.name, dataUrl);
    if (/\.(png|jpe?g|webp)$/i.test(file.name)) { setImage(saved, dataUrl); openStep("create"); }
    else importPath(saved);
  });
}

// ------------------------------------------------------------------ assistant
const messages = $("#messages");
const chatEmptyHTML = messages.innerHTML;
const clearChat = () => (messages.innerHTML = chatEmptyHTML);
messages.addEventListener("click", (e) => {
  const b = e.target.closest(".suggest");
  if (b) { $("#chatInput").value = b.textContent; $("#chatInput").focus(); }
  const copy = e.target.closest(".copy");
  if (copy) {
    navigator.clipboard?.writeText(copy.parentElement.querySelector("code").textContent);
    copy.title = "Copié";
    copy.classList.add("done");
    setTimeout(() => copy.classList.remove("done"), 1200);
  }
});

async function loadChatHistory() {
  if (!backend) return;
  const items = JSON.parse(await call("getChatHistory"));
  for (const ev of items) {
    if (ev.sub === "user") addMsg("user", ev.text);
    else agentEvent(ev, { restoring: true });
  }
}

const escapeHtml = (t) => t.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** Markdown minimal et sûr : blocs de code, `code`, **gras**, listes. */
function markdown(text) {
  const parts = text.split(/```(?:[\w-]+)?\n?([\s\S]*?)```/g);
  return parts.map((part, i) => {
    if (i % 2 === 0) part = fr(part);
    if (i % 2) {
      return `<div class="code"><button class="copy" type="button" title="Copier" aria-label="Copier le code"><svg viewBox="0 0 24 24"><use href="#i-copy"/></svg></button><pre><code>${escapeHtml(part.replace(/\n$/, ""))}</code></pre></div>`;
    }
    let html = escapeHtml(part)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
    html = html.replace(/(?:^|\n)((?:[-*] .*(?:\n|$))+)/g, (m, block) =>
      `\n<ul>${block.trim().split("\n").map((l) => `<li>${l.replace(/^[-*] /, "")}</li>`).join("")}</ul>\n`);
    return html.trim().split(/\n{2,}/).map((p) => (p.startsWith("<ul>") ? p : `<p>${p.replace(/\n/g, "<br>")}</p>`)).join("");
  }).join("");
}

function addMsg(cls, text, { html = false } = {}) {
  $("#chatEmpty")?.remove();
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  if (html) div.innerHTML = text;
  else div.textContent = fr(text);
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
  if (ev.sub === "assistant") addMsg("assistant", markdown(ev.text), { html: true });
  else if (ev.sub === "guard") addMsg("guard", ev.text);
  else if (ev.sub === "tool") {
    lastTool = addMsg("tool", "");
    const detail = ev.input?.explanation || (ev.input?.step ? `étape ${ev.input.step}` : ev.input?.name || "");
    const d = document.createElement("details");
    const s = document.createElement("summary");
    s.textContent = `${TOOL_LABELS[ev.name] || ev.name}${detail ? " : " + detail : ""}`;
    d.append(s);
    lastTool.append(d);
  } else if (ev.sub === "tool_result" && lastTool) {
    lastTool.classList.add(ev.error ? "err" : "ok");
    const pre = document.createElement("pre");
    pre.textContent = ev.text;
    $("details", lastTool).append(pre);
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
$("#stopBtn").onclick = () => backend.cancel();
$("#resetChatBtn").onclick = () => {
  clearChat();
  backend.resetChat();
};

// ------------------------------------------------------------------ occupé / journal / toasts
let jobTimer = null;
let busyNow = false;

function setBusy(on, label, started) {
  busyNow = !!on;
  $("#progress").hidden = !on;
  $("#job").hidden = !on;
  $("#stopBtn").hidden = !on;
  $$("#generateBtn, #importBtn, .export, #sendBtn").forEach((b) => (b.disabled = !!on));
  if (on) {
    $$("#reimportBtn, #rigBtn, #animateBtn").forEach((b) => (b.disabled = true));
  } else if (state) {
    const info = state.project.info || {};
    $("#reimportBtn").disabled = !info.source;
    $("#rigBtn").disabled = !info.mesh;
    $("#animateBtn").disabled = !info.rig;
  }
  clearInterval(jobTimer);
  if (on) {
    if (label) {
      $("#jobLabel").textContent = label;
      addLog(`${label}…`);
    }
    const t0 = (started || Date.now() / 1000) * 1000;
    const tickTime = () => {
      const s = Math.max(0, Math.round((Date.now() - t0) / 1000));
      $("#jobTime").textContent = s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")}`;
    };
    tickTime();
    jobTimer = setInterval(tickTime, 1000);
  }
}

function addLog(text, level = "info") {
  const time = new Date().toLocaleTimeString("fr-FR");
  $("#journalLast").textContent = text.split("\n")[0];
  $("#journalLast").className = level === "error" ? "error" : "";
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

const TOAST_ACTIONS = {
  settings: ["Ouvrir les réglages", () => openSettings()],
  folder: ["Ouvrir le dossier", () => backend.openFolder()],
  blender: ["Ouvrir Blender", () => backend.openBlender()],
};

function toast(text, level = "info", action = null) {
  const t = document.createElement("div");
  t.className = `toast ${level}`;
  const p = document.createElement("span");
  p.textContent = fr(text);
  t.append(p);
  if (action && TOAST_ACTIONS[action]) {
    const [label, fn] = TOAST_ACTIONS[action];
    const b = document.createElement("button");
    b.className = "toast-action";
    b.textContent = label;
    b.onclick = () => { fn(); t.remove(); };
    t.append(b);
  }
  const close = document.createElement("button");
  close.className = "toast-close";
  close.setAttribute("aria-label", "Fermer");
  close.innerHTML = '<svg viewBox="0 0 24 24"><use href="#i-x"/></svg>';
  close.onclick = () => t.remove();
  t.append(close);
  $("#toasts").append(t);
  // Les erreurs restent plus longtemps ; une notification survolée ne disparaît pas.
  let left = level === "error" ? 10000 : action ? 7000 : 4500;
  const timer = setInterval(() => {
    if (!t.matches(":hover")) left -= 250;
    if (left <= 0) { clearInterval(timer); t.remove(); }
  }, 250);
  while ($("#toasts").children.length > 4) $("#toasts").firstElementChild.remove();
}

// ------------------------------------------------------------------ réglages
const dlg = $("#settingsDialog");
const form = $("#settingsForm");

function openSettings() {
  if (!state) return;
  const st = state.settings;
  for (const el of form.elements) {
    if (!el.name || el.name === "api_key") continue;
    if (el.type === "radio") el.checked = st[el.name] === el.value;
    else if (el.type === "checkbox") el.checked = !!st[el.name];
    else if (el.name in st) el.value = st[el.name];
  }
  form.elements.api_key.value = "";
  $("#keyState").textContent = state.has_api_key ? "(déjà enregistrée)" : "(manquante)";
  $$(".test-result", form).forEach((r) => (r.hidden = true));
  syncWhen();
  if (!dlg.open) dlg.showModal();
}
$("#settingsBtn").onclick = openSettings;

function syncWhen() {
  const data = new FormData(form);
  $$(".when", form).forEach((el) => {
    const [k, v] = el.dataset.when.split("=");
    el.classList.toggle("on", data.get(k) === v);
  });
}
form.addEventListener("change", syncWhen);

function formValues() {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "radio") { if (el.checked) out[el.name] = el.value; }
    else if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "number") out[el.name] = Number(el.value);
    else out[el.name] = el.value;
  }
  return out;
}

$$(".test", form).forEach((b) => {
  b.onclick = () => {
    const r = $(`.test-result[data-result="${b.dataset.test}"]`, form);
    r.hidden = false;
    r.className = "test-result pending";
    r.textContent = "Test en cours…";
    b.disabled = true;
    backend.testConnection(b.dataset.test, JSON.stringify(formValues()));
  };
});

function showTestResult(ev) {
  const r = $(`.test-result[data-result="${ev.test}"]`, form);
  const b = $(`.test[data-test="${ev.test}"]`, form);
  if (b) b.disabled = false;
  if (!r) return;
  r.hidden = false;
  r.className = `test-result ${ev.ok ? "ok" : "ko"}`;
  r.textContent = ev.message;
}

form.addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "save") return;
  e.preventDefault();
  toast(await call("saveSettings", JSON.stringify(formValues())));
  dlg.close();
});

// ------------------------------------------------------------------ raccourcis clavier
const typing = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));

document.addEventListener("keydown", (e) => {
  const dialogOpen = !!$("dialog[open]");
  const mod = e.ctrlKey || e.metaKey;

  if (mod && e.key.toLowerCase() === "n") { e.preventDefault(); openProjects(); return; }
  if (mod && e.key.toLowerCase() === "o") { e.preventDefault(); if (!busyNow) importFile(); return; }
  if (mod && e.key === ",") { e.preventDefault(); openSettings(); return; }
  if (dialogOpen) return; // Échap ferme déjà les fenêtres

  if (e.key === "Escape" && busyNow) { e.preventDefault(); backend.cancel(); return; }
  if (e.altKey && /^[1-5]$/.test(e.key)) { e.preventDefault(); openStep(STEP_ORDER[Number(e.key) - 1]); return; }
  if (typing(document.activeElement) || mod || e.altKey) return;

  const key = e.key.toLowerCase();
  if (key === " " && $("#stage").classList.contains("has-model")) { e.preventDefault(); setPlayIcon(viewer.togglePause()); }
  else if (key === "f") viewer.frame();
  else if (key === "k") setTool("skeleton", !viewer.options.skeleton);
  else if (key === "w") setTool("wireframe", !viewer.options.wireframe);
  else if (key === "r") setTool("turntable", !viewer.options.turntable);
  else if (key === "p") capture();
  else if (key === "/") { e.preventDefault(); $("#chatInput").focus(); }
  else if (key === "?") $("#keysDialog").showModal();
});
