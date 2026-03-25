import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

/**
 * Puffy popcorn kernel — merged overlapping icosahedrons.
 * @param {number} detail - Icosahedron subdivision level (1 = low poly, 2 = smooth)
 */
export function createPopcornGeometry(detail = 2) {
  const parts = [];
  parts.push(new THREE.IcosahedronGeometry(0.04, detail));
  const puffCount = 4 + Math.floor(Math.random() * 3);
  for (let i = 0; i < puffCount; i++) {
    const puff = new THREE.IcosahedronGeometry(0.025 + Math.random() * 0.015, detail);
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    const dist = 0.025 + Math.random() * 0.01;
    puff.translate(
      Math.sin(phi) * Math.cos(theta) * dist,
      Math.sin(phi) * Math.sin(theta) * dist,
      Math.cos(phi) * dist
    );
    parts.push(puff);
  }
  const merged = mergeGeometries(parts);
  if (!merged) return new THREE.IcosahedronGeometry(0.04, detail);
  merged.computeVertexNormals();
  merged.computeBoundingBox();
  merged.center();
  return merged;
}
