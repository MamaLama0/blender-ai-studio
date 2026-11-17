// Aperçu 3D : un plateau tournant dans un studio, le modèle .glb et ses animations.
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

export class Viewer {
  constructor(canvas) {
    this.canvas = canvas;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.01, 100);
    this.camera.position.set(2.1, 1.45, 3.2);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.target.set(0, 0.5, 0);

    this.scene.add(new THREE.HemisphereLight(0xdfe7ff, 0x2a2622, 1.1));
    const key = new THREE.DirectionalLight(0xfff1e0, 2.4);
    key.position.set(2.5, 4, 3);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.camera.left = key.shadow.camera.bottom = -2;
    key.shadow.camera.right = key.shadow.camera.top = 2;
    key.shadow.bias = -0.0005;
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(0x8fd3cf, 1.2);
    rim.position.set(-3, 2, -3);
    this.scene.add(rim);

    // Le plateau : un disque d'argile qui reçoit l'ombre du personnage
    const disc = new THREE.Mesh(
      new THREE.CylinderGeometry(0.7, 0.74, 0.04, 96),
      new THREE.MeshStandardMaterial({ color: 0x2b3039, roughness: 0.92 })
    );
    disc.position.y = -0.02;
    disc.receiveShadow = true;
    this.scene.add(disc);
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(0.725, 0.737, 128),
      new THREE.MeshBasicMaterial({ color: 0xf28c28, transparent: true, opacity: 0.55, side: THREE.DoubleSide })
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.001;
    this.scene.add(ring);
    this.disc = disc;

    this.timer = new THREE.Timer();
    this.mixer = null;
    this.actions = {};
    this.current = null;
    this.paused = false;
    this.model = null;

    new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
    this.resize();
    this.renderer.setAnimationLoop(() => this.tick());
  }

  resize() {
    const { clientWidth: w, clientHeight: h } = this.canvas.parentElement;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  tick() {
    this.timer.update();
    const dt = this.timer.getDelta();
    if (this.mixer && !this.paused) this.mixer.update(dt);
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  async load(url) {
    const gltf = await new GLTFLoader().loadAsync(url);
    if (this.model) this.scene.remove(this.model);
    this.model = gltf.scene;
    this.model.traverse((o) => {
      if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; }
    });

    // Cadrage : le personnage fait ~1 m après l'import, mais on reste robuste.
    const box = new THREE.Box3().setFromObject(this.model);
    const size = box.getSize(new THREE.Vector3());
    const height = size.y || 1;
    const scale = height > 0 ? 1 / height : 1;
    this.model.scale.setScalar(scale);
    const center = new THREE.Box3().setFromObject(this.model).getCenter(new THREE.Vector3());
    this.model.position.x -= center.x;
    this.model.position.z -= center.z;
    this.model.position.y -= new THREE.Box3().setFromObject(this.model).min.y;
    this.scene.add(this.model);

    if (!this._framed) {
      this.controls.target.set(0, 0.5, 0);
      this.camera.position.set(1.9, 1.3, 2.9);
      this._framed = true;
    }

    this.mixer = new THREE.AnimationMixer(this.model);
    this.actions = {};
    for (const clip of gltf.animations) this.actions[clip.name] = this.mixer.clipAction(clip);
    const names = Object.keys(this.actions);
    this.current = null;
    if (names.length) this.play(names.includes("Walk") ? "Walk" : names[0]);
    return names;
  }

  play(name) {
    const next = this.actions[name];
    if (!next) return;
    if (this.current && this.current !== next) this.current.fadeOut(0.25);
    next.reset().fadeIn(0.25).play();
    this.current = next;
    this.paused = false;
  }

  togglePause() {
    this.paused = !this.paused;
    return this.paused;
  }
}
