import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { gltfLoader } from './loaders.js';
import { FontLoader } from 'three/addons/loaders/FontLoader.js';
import { TextGeometry } from 'three/addons/geometries/TextGeometry.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { RapierPhysics } from './RapierPhysics.js';
import { createPopcornGeometry } from './geometry.js';
import { NEON_BLUE, BG_DARK, AMBER, CARPET_RED, POPCORN_CREAM } from './theme.js';
const INSTANCE_COUNT = 500;
const MACHINE_X = 1.5;

export async function createScene(container) {
  if (!container.clientWidth || !container.clientHeight) {
    throw new Error('Scene container has zero dimensions');
  }

  // ── Renderer ──
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.VSMShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  container.appendChild(renderer.domElement);

  // ── Scene ──
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(BG_DARK);

  // ── Camera ──
  const camera = new THREE.PerspectiveCamera(
    45, container.clientWidth / container.clientHeight, 0.1, 100
  );
  camera.position.set(MACHINE_X, 2.5, 7);

  // ── Orbit Controls ──
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;
  controls.minDistance = 3;
  controls.maxDistance = 12;
  controls.maxPolarAngle = Math.PI / 2;
  controls.target.set(MACHINE_X, 1.2, 0);
  controls.enablePan = false;
  controls.enableZoom = false;
  controls.autoRotate = false;

  // Orbit-active guard for scatter
  let isOrbitControlsActive = false;
  controls.addEventListener('start', () => { isOrbitControlsActive = true; });
  controls.addEventListener('end', () => {
    isOrbitControlsActive = false;
    // Recalculate sway base from where the user left the camera
    updateSwayBase();
  });
  controls.touches = { ONE: null, TWO: THREE.TOUCH.DOLLY_PAN };

  // ── Lighting ──
  const ambient = new THREE.AmbientLight(0x111122, 0.4);
  scene.add(ambient);

  const carpetLight = new THREE.SpotLight(AMBER, 5);
  carpetLight.position.set(MACHINE_X, 4, 0);
  carpetLight.target.position.set(MACHINE_X, 0, 0);
  carpetLight.angle = Math.PI / 3;
  carpetLight.penumbra = 0.9;
  carpetLight.distance = 8;
  scene.add(carpetLight);
  scene.add(carpetLight.target);

  const topLight = new THREE.SpotLight(0xffffff, 4);
  topLight.position.set(MACHINE_X, 6, 3);
  topLight.target.position.set(MACHINE_X, 0.5, 0);
  topLight.angle = Math.PI / 4;
  topLight.penumbra = 0.8;
  topLight.castShadow = true;
  topLight.shadow.mapSize.set(1024, 1024);
  topLight.shadow.bias = -0.0005;
  scene.add(topLight);
  scene.add(topLight.target);

  const frontLight = new THREE.PointLight(0x6688AA, 3);
  frontLight.position.set(MACHINE_X + 2, 2, 4);
  scene.add(frontLight);

  // ── Red Carpet Floor ──
  const floorGeo = new THREE.CircleGeometry(8, 64);
  floorGeo.rotateX(-Math.PI / 2);
  const floor = new THREE.Mesh(floorGeo, new THREE.MeshStandardMaterial({
    color: CARPET_RED, roughness: 0.85, metalness: 0.0,
  }));
  floor.receiveShadow = true;
  scene.add(floor);

  // ── Physics Floor Collider ──
  const physFloor = new THREE.Mesh(
    new THREE.BoxGeometry(10, 5, 10),
    new THREE.MeshStandardMaterial()
  );
  physFloor.position.set(0, -2.5, 0);
  physFloor.visible = false;
  physFloor.userData.physics = { mass: 0, friction: 0.8 };
  scene.add(physFloor);

  // ── Parallel Loading ──
  const loadMachine = () => new Promise((resolve) => {
    gltfLoader.load('/models/popcorn_machine_v1.glb', (gltf) => {
      const model = gltf.scene;
      model.traverse((child) => {
        if (child.isMesh) {
          child.castShadow = true;
          child.receiveShadow = true;
          if (child.material) {
            child.material.metalness = Math.min(child.material.metalness, 0.2);
            child.material.roughness = Math.max(child.material.roughness, 0.5);
            if (child.material.color) {
              const lum = child.material.color.r * 0.299 + child.material.color.g * 0.587 + child.material.color.b * 0.114;
              if (lum < 0.15) child.material.color.multiplyScalar(2.5);
            }
          }
        }
      });

      const box = new THREE.Box3().setFromObject(model);
      const size = new THREE.Vector3();
      box.getSize(size);
      model.scale.setScalar(2.8 / size.y);

      box.setFromObject(model);
      const center = new THREE.Vector3();
      box.getCenter(center);
      model.position.set(MACHINE_X - center.x, -box.min.y, -center.z);

      scene.add(model);
      resolve(model);
    }, undefined, (err) => { console.error('Machine model failed:', err); resolve(null); });
  });

  const loadNeonFont = () => new Promise((resolve) => {
    new FontLoader().load('/fonts/ht-neon-regular.json', resolve, undefined, (err) => { console.error('Neon font failed:', err); resolve(null); });
  });

  const [physics, machineModel, neonFont] = await Promise.all([
    RapierPhysics(),
    loadMachine(),
    loadNeonFont(),
  ]);

  // ── Popcorn InstancedMesh ──
  const popcornGeo = createPopcornGeometry();
  const popcornMat = new THREE.MeshStandardMaterial({
    color: POPCORN_CREAM, roughness: 0.9, metalness: 0.0,
  });
  const popcorn = new THREE.InstancedMesh(popcornGeo, popcornMat, INSTANCE_COUNT);
  popcorn.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  popcorn.castShadow = true;
  popcorn.receiveShadow = true;
  popcorn.frustumCulled = false;

  // Natural cream — scene lighting tints them blue/amber
  const tintColor = new THREE.Color();
  for (let i = 0; i < INSTANCE_COUNT; i++) {
    const warm = 0.9 + Math.random() * 0.1;
    tintColor.setRGB(warm, warm * 0.97, warm * 0.92);
    popcorn.setColorAt(i, tintColor);
  }

  // Initial positions above machine
  const dummy = new THREE.Object3D();
  for (let i = 0; i < INSTANCE_COUNT; i++) {
    dummy.position.set(
      MACHINE_X + (Math.random() - 0.5) * 0.3,
      3 + Math.random() * 2,
      (Math.random() - 0.5) * 0.3
    );
    dummy.updateMatrix();
    popcorn.setMatrixAt(i, dummy.matrix);
  }

  popcorn.userData.physics = { mass: 1, restitution: 0, friction: 0.8, colliderRadius: 0.05 };
  scene.add(popcorn);

  // ── Machine Trimesh Collider ──
  if (machineModel) {
    const geos = [];
    machineModel.traverse((child) => {
      if (child.isMesh) {
        const geo = child.geometry.clone();
        child.updateWorldMatrix(true, false);
        geo.applyMatrix4(child.matrixWorld);
        geos.push(geo);
      }
    });
    if (geos.length > 0) {
      const mergedGeo = mergeGeometries(geos);
      if (mergedGeo) {
        const collider = new THREE.Mesh(mergedGeo, new THREE.MeshStandardMaterial());
        collider.visible = false;
        collider.userData.physics = { mass: 0 };
        scene.add(collider);
        physics.addMesh(collider, 0, 0.2);
      }
    }
  }

  // ── Activate Physics ──
  physics.addScene(scene);

  // ── Spawn Loop — matches physics_rapier_instancing pattern ──
  let spawnY = 3.0;
  if (machineModel) {
    spawnY = new THREE.Box3().setFromObject(machineModel).max.y + 0.3;
  }
  const spawnPos = new THREE.Vector3();

  const spawnVel = new THREE.Vector3();
  let nextSpawnIndex = 0;  // round-robin — always recycles the oldest piece

  function spawnFn() {
    const index = nextSpawnIndex;
    nextSpawnIndex = (nextSpawnIndex + 1) % INSTANCE_COUNT;
    const angle = Math.random() * Math.PI * 2;
    const behavior = Math.random();

    spawnPos.set(
      MACHINE_X + Math.sin(angle) * 0.15,
      spawnY + Math.random() * 0.3,
      Math.cos(angle) * 0.15
    );
    physics.setMeshPosition(popcorn, spawnPos, index);

    if (behavior < 0.4) {
      // 40% — gentle drop
    } else if (behavior < 0.75) {
      const speed = 1.5 + Math.random() * 2;
      spawnVel.set(Math.sin(angle) * 0.8, speed, Math.cos(angle) * 0.8);
      physics.setMeshVelocity(popcorn, spawnVel, index);
    } else {
      const speed = 3 + Math.random() * 3;
      spawnVel.set((Math.random() - 0.5) * 1.2, speed, (Math.random() - 0.5) * 1.2);
      physics.setMeshVelocity(popcorn, spawnVel, index);
    }
  }
  let spawnInterval = setInterval(spawnFn, 1000 / 30);

  // ── Neon Sign — bigger ──
  if (neonFont) {
    const textGeo = new TextGeometry('BluePopcorn', {
      font: neonFont,
      size: 0.75,
      depth: 0.06,
      curveSegments: 6,
      bevelEnabled: true,
      bevelThickness: 0.025,
      bevelSize: 0.018,
    });
    textGeo.center();

    const neonMat = new THREE.MeshStandardMaterial({
      color: NEON_BLUE,
      emissive: new THREE.Color(NEON_BLUE),
      emissiveIntensity: 1.2,
      toneMapped: false,
    });

    const sign = new THREE.Mesh(textGeo, neonMat);
    const signY = machineModel
      ? new THREE.Box3().setFromObject(machineModel).max.y + 0.05
      : 2.5;
    sign.position.set(MACHINE_X, signY, -0.5);
    sign.castShadow = false;
    scene.add(sign);

    const signLight = new THREE.PointLight(NEON_BLUE, 4);
    signLight.position.copy(sign.position);
    scene.add(signLight);
  }

  // ── Mouse Scatter — matches compute_particles pattern ──
  const groundPlane = new THREE.Mesh(
    new THREE.PlaneGeometry(200, 200),
    new THREE.MeshBasicMaterial({ visible: false })
  );
  groundPlane.rotateX(-Math.PI / 2);
  groundPlane.position.y = 0.05;
  scene.add(groundPlane);

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const scatterVel = new THREE.Vector3();
  const instancePos = new THREE.Vector3();
  const matrix = new THREE.Matrix4();
  let scatterDirty = false;
  let scatterPoint = new THREE.Vector3();

  // Cursor spotlight — follows mouse position on the ground
  const cursorLight = new THREE.PointLight(NEON_BLUE, 0, 3);
  cursorLight.position.set(0, 0.3, 0);
  scene.add(cursorLight);

  function onPointerMove(event) {
    if (isOrbitControlsActive) return;

    // Use canvas bounds for correct NDC even when canvas doesn't fill viewport
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.set(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1
    );

    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObject(groundPlane);

    if (hits.length > 0) {
      scatterPoint.copy(hits[0].point);
      scatterDirty = true;
      // Move cursor spotlight to hit point
      cursorLight.position.set(scatterPoint.x, 0.3, scatterPoint.z);
      cursorLight.intensity = 2;
    } else {
      cursorLight.intensity = 0;
    }
  }

  // Hide cursor light when mouse leaves canvas
  function onPointerLeave() { cursorLight.intensity = 0; }
  renderer.domElement.addEventListener('pointerleave', onPointerLeave);

  // Combined OOB recycling + mouse scatter — single pass over all instances
  const SCATTER_RADIUS = 0.8;

  function processInstances() {
    const doScatter = scatterDirty;
    if (doScatter) scatterDirty = false;

    for (let i = 0; i < INSTANCE_COUNT; i++) {
      popcorn.getMatrixAt(i, matrix);
      instancePos.setFromMatrixPosition(matrix);

      // OOB recycling — below floor, too far from center, or way above
      if (instancePos.y < -1 || instancePos.lengthSq() > 100 || instancePos.y > 8) {
        const angle = Math.random() * Math.PI * 2;
        instancePos.set(
          MACHINE_X + Math.sin(angle) * 0.15,
          spawnY + Math.random() * 0.3,
          Math.cos(angle) * 0.15
        );
        physics.setMeshPosition(popcorn, instancePos, i);
        continue;
      }

      // Mouse scatter
      if (doScatter) {
        const dx = instancePos.x - scatterPoint.x;
        const dz = instancePos.z - scatterPoint.z;
        const distSq = dx * dx + dz * dz;

        if (distSq < SCATTER_RADIUS * SCATTER_RADIUS && distSq > 0.0001) {
          if (instancePos.y <= scatterPoint.y + 1.5) {
            const dist = Math.sqrt(distSq);
            const strength = (SCATTER_RADIUS - dist) / SCATTER_RADIUS;
            const pushForce = 3 + strength * 4.5;
            const liftForce = 1.5 + strength * 3.5;

            scatterVel.set(
              (dx / dist) * pushForce,
              liftForce,
              (dz / dist) * pushForce
            );
            physics.setMeshVelocity(popcorn, scatterVel, i);
          }
        }
      }
    }
  }

  renderer.domElement.addEventListener('pointermove', onPointerMove);

  // ── Postprocessing ──
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  composer.addPass(new UnrealBloomPass(
    new THREE.Vector2(container.clientWidth, container.clientHeight),
    0.5, 0.4, 1.0
  ));
  composer.addPass(new OutputPass());

  // ── Animation Loop — cinematic slow sway ──
  let animationId;
  let camBaseAngleH, camBaseAngleV, camRadius;
  let swayTimeOffset = 0;

  function updateSwayBase() {
    camRadius = camera.position.distanceTo(controls.target);
    camBaseAngleH = Math.atan2(
      camera.position.x - controls.target.x,
      camera.position.z - controls.target.z
    );
    camBaseAngleV = Math.asin(
      (camera.position.y - controls.target.y) / camRadius
    );
    // Sync sway phase so it continues smoothly from current position
    swayTimeOffset = performance.now() * 0.0001;
  }
  updateSwayBase();

  function animate() {
    animationId = requestAnimationFrame(animate);

    // Super slow cinematic sway — continues from where user left off
    if (!isOrbitControlsActive) {
      const t = performance.now() * 0.0001 - swayTimeOffset;
      const swayH = Math.sin(t) * 0.218;       // ±12.5° horizontal
      const swayV = Math.sin(t * 0.7) * 0.052;  // ±3° vertical

      const angleH = camBaseAngleH + swayH;
      const angleV = camBaseAngleV + swayV;

      camera.position.set(
        controls.target.x + Math.sin(angleH) * Math.cos(angleV) * camRadius,
        controls.target.y + Math.sin(angleV) * camRadius,
        controls.target.z + Math.cos(angleH) * Math.cos(angleV) * camRadius
      );
    }

    processInstances();
    controls.update();
    composer.render();
  }
  animate();

  // ── Resize ──
  function onResize() {
    const w = container.clientWidth;
    const h = container.clientHeight;
    if (!w || !h) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
    composer.setSize(w, h);
  }
  window.addEventListener('resize', onResize);

  // ── Cleanup ──
  function dispose() {
    window.removeEventListener('resize', onResize);
    renderer.domElement.removeEventListener('pointermove', onPointerMove);
    renderer.domElement.removeEventListener('pointerleave', onPointerLeave);
    cancelAnimationFrame(animationId);
    clearInterval(spawnInterval);
    physics.dispose();
    controls.dispose();
    // Free GPU resources
    scene.traverse((obj) => {
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) {
        const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
        mats.forEach((m) => { m.dispose(); if (m.map) m.map.dispose(); });
      }
    });
    renderer.dispose();
    composer.dispose();
    if (renderer.domElement.parentNode) {
      renderer.domElement.parentNode.removeChild(renderer.domElement);
    }
  }

  // ── Pause/Resume — stop rendering when off-screen ──
  let paused = false;

  function pause() {
    if (paused) return;
    paused = true;
    cancelAnimationFrame(animationId);
    clearInterval(spawnInterval);
    physics.pause();
  }

  function resume() {
    if (!paused) return;
    paused = false;
    physics.resume();
    animationId = requestAnimationFrame(animate);
    spawnInterval = setInterval(spawnFn, 1000 / 30);
  }

  return { dispose, pause, resume };
}
