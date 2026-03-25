/**
 * Scroll-reactive floating props overlay.
 * Loads popcorn kernels + movie theater props from GLB.
 * Popcorn gets light blue tint, other props keep natural colors.
 */
import * as THREE from 'three';
import { gltfLoader } from './loaders.js';
import { createPopcornGeometry } from './geometry.js';
import { POPCORN_OVERLAY } from './theme.js';

const KERNEL_COUNT = 25;
const PROP_COUNT = 8;

export function createParticleOverlay(container) {
  if (!container.clientWidth || !container.clientHeight) {
    return { render() {}, dispose() {} };
  }

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.setClearColor(0x000000, 0);
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();

  const aspect = container.clientWidth / container.clientHeight;
  const frustumSize = 10;
  const camera = new THREE.OrthographicCamera(
    -frustumSize * aspect / 2, frustumSize * aspect / 2,
    frustumSize / 2, -frustumSize / 2, 0.1, 100
  );
  camera.position.z = 10;

  // Lighting — neutral/warm so props look natural
  scene.add(new THREE.AmbientLight(0x333333, 1.2));
  const key = new THREE.DirectionalLight(0xffffff, 1.0);
  key.position.set(2, 3, 5);
  scene.add(key);
  const warm = new THREE.DirectionalLight(0xFFCC88, 0.5);
  warm.position.set(-3, -1, 3);
  scene.add(warm);

  const halfW = (frustumSize * aspect) / 2;
  const halfH = frustumSize / 2;

  const items = [];
  let startTime = 0;

  function addFloatingItem(mesh, activateDelay) {
    const side = Math.random();
    let x;
    if (side < 0.35) x = -halfW + Math.random() * 2.5;
    else if (side < 0.7) x = halfW - Math.random() * 2.5;
    else x = (Math.random() - 0.5) * halfW * 1.2;

    const y = (Math.random() - 0.5) * halfH * 6;
    const targetScale = mesh.scale.x;
    mesh.position.set(x, y, Math.random() * 4);
    mesh.scale.setScalar(0);
    mesh.visible = false;
    scene.add(mesh);

    const driftAngle = Math.random() * Math.PI * 2;
    const driftSpeed = 0.1 + Math.random() * 0.2;

    const onScreen = Math.abs(y) < halfH;
    items.push({
      mesh,
      targetScale,
      startX: x,
      startY: y,
      driftX: Math.cos(driftAngle) * driftSpeed,
      driftY: Math.sin(driftAngle) * driftSpeed,
      rotSpeedX: (Math.random() - 0.5) * 0.15,
      rotSpeedY: (Math.random() - 0.5) * 0.1,
      rotSpeedZ: (Math.random() - 0.5) * 0.12,
      activateAt: onScreen ? 0 : activateDelay,
    });
  }

  // Add popcorn kernels (light blue tint)
  const popcornMat = new THREE.MeshStandardMaterial({
    color: POPCORN_OVERLAY,
    roughness: 0.85,
    metalness: 0,
  });
  const geoVariants = Array.from({ length: 5 }, () => createPopcornGeometry(1));
  for (let i = 0; i < KERNEL_COUNT; i++) {
    const mesh = new THREE.Mesh(geoVariants[i % geoVariants.length], popcornMat);
    mesh.scale.setScalar(0.7 + Math.random() * 0.6);
    addFloatingItem(mesh, i * 0.15);
  }

  // Load GLB props (hotdog, drink, popcorn box) — keep original materials
  gltfLoader.load('/models/movie_theater_props_v1.glb', (gltf) => {
    const propMeshes = [];
    gltf.scene.traverse((child) => {
      if (child.isMesh) propMeshes.push(child);
    });

    const usableProps = propMeshes.filter(m =>
      m.geometry.getAttribute('position').count > 1000
    );

    if (usableProps.length === 0) return;

    for (let i = 0; i < PROP_COUNT; i++) {
      const src = usableProps[i % usableProps.length];
      const mesh = new THREE.Mesh(src.geometry.clone(), src.material.clone());

      const box = new THREE.Box3().setFromBufferAttribute(mesh.geometry.getAttribute('position'));
      const size = new THREE.Vector3();
      box.getSize(size);
      const maxDim = Math.max(size.x, size.y, size.z);
      const s = 0.5 / maxDim;
      mesh.geometry.scale(s, s, s);
      mesh.geometry.center();
      mesh.scale.setScalar(0.8 + Math.random() * 0.7);

      addFloatingItem(mesh, KERNEL_COUNT * 0.15 + i * 0.3);
    }
  }, undefined, (err) => console.error('Props GLB failed to load:', err));

  function render(time) {
    const t = time * 0.001;
    if (!startTime) startTime = t;
    const elapsed = t - startTime;

    for (const k of items) {
      const rawFade = Math.min(1, Math.max(0, (elapsed - k.activateAt) / 1.5));
      if (rawFade <= 0) { k.mesh.visible = false; continue; }

      const fadeIn = 1 - Math.pow(1 - rawFade, 3);

      k.mesh.visible = true;
      k.mesh.scale.setScalar(k.targetScale * fadeIn);

      let x = k.startX + elapsed * k.driftX;
      let y = k.startY + elapsed * k.driftY;

      if (x < -halfW - 1) x += halfW * 2 + 2;
      if (x > halfW + 1) x -= halfW * 2 + 2;
      if (y < -halfH - 1) y += halfH * 2 + 2;
      if (y > halfH + 1) y -= halfH * 2 + 2;

      k.mesh.position.x = x;
      k.mesh.position.y = y;

      k.mesh.rotation.x = elapsed * k.rotSpeedX;
      k.mesh.rotation.y = elapsed * k.rotSpeedY;
      k.mesh.rotation.z = elapsed * k.rotSpeedZ;
    }

    renderer.render(scene, camera);
  }

  function onResize() {
    const w = container.clientWidth;
    const h = container.clientHeight;
    if (!w || !h) return;
    const a = w / h;
    camera.left = -frustumSize * a / 2;
    camera.right = frustumSize * a / 2;
    camera.top = frustumSize / 2;
    camera.bottom = -frustumSize / 2;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }
  window.addEventListener('resize', onResize);

  function dispose() {
    window.removeEventListener('resize', onResize);
    // Free all GPU resources
    for (const item of items) {
      item.mesh.geometry.dispose();
      if (item.mesh.material.dispose) item.mesh.material.dispose();
      if (item.mesh.material.map) item.mesh.material.map.dispose();
    }
    geoVariants.forEach(g => g.dispose());
    popcornMat.dispose();
    renderer.dispose();
    if (renderer.domElement.parentNode) {
      renderer.domElement.parentNode.removeChild(renderer.domElement);
    }
  }

  return { render, dispose };
}
