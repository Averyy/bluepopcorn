/**
 * Local copy of Three.js RapierPhysics addon.
 * Modified to import @dimforge/rapier3d-compat from node_modules
 * instead of fetching from CDN.
 */
import { Timer, Vector3, Quaternion, Matrix4 } from 'three';

const frameRate = 60;

const _scale = new Vector3( 1, 1, 1 );
const ZERO = new Vector3();

let RAPIER = null;
let initPromise = null;

function getShape( geometry ) {

	const parameters = geometry.parameters;

	if ( geometry.type === 'RoundedBoxGeometry' ) {

		const sx = parameters.width !== undefined ? parameters.width / 2 : 0.5;
		const sy = parameters.height !== undefined ? parameters.height / 2 : 0.5;
		const sz = parameters.depth !== undefined ? parameters.depth / 2 : 0.5;
		const radius = parameters.radius !== undefined ? parameters.radius : 0.1;

		return RAPIER.ColliderDesc.roundCuboid( sx - radius, sy - radius, sz - radius, radius );

	} else if ( geometry.type === 'BoxGeometry' ) {

		const sx = parameters.width !== undefined ? parameters.width / 2 : 0.5;
		const sy = parameters.height !== undefined ? parameters.height / 2 : 0.5;
		const sz = parameters.depth !== undefined ? parameters.depth / 2 : 0.5;

		return RAPIER.ColliderDesc.cuboid( sx, sy, sz );

	} else if ( geometry.type === 'SphereGeometry' || geometry.type === 'IcosahedronGeometry' ) {

		const radius = parameters.radius !== undefined ? parameters.radius : 1;
		return RAPIER.ColliderDesc.ball( radius );

	} else if ( geometry.type === 'CylinderGeometry' ) {

		const radius = parameters.radiusBottom !== undefined ? parameters.radiusBottom : 0.5;
		const length = parameters.height !== undefined ? parameters.height : 0.5;

		return RAPIER.ColliderDesc.cylinder( length / 2, radius );

	} else if ( geometry.type === 'CapsuleGeometry' ) {

		const radius = parameters.radius !== undefined ? parameters.radius : 0.5;
		const length = parameters.height !== undefined ? parameters.height : 0.5;

		return RAPIER.ColliderDesc.capsule( length / 2, radius );

	} else if ( geometry.type === 'BufferGeometry' ) {

		const vertices = [];
		const vertex = new Vector3();
		const position = geometry.getAttribute( 'position' );

		for ( let i = 0; i < position.count; i ++ ) {

			vertex.fromBufferAttribute( position, i );
			vertices.push( vertex.x, vertex.y, vertex.z );

		}

		const indices = geometry.getIndex() === null
			? Uint32Array.from( Array( parseInt( vertices.length / 3 ) ).keys() )
			: geometry.getIndex().array;

		return RAPIER.ColliderDesc.trimesh( vertices, indices );

	}

	console.error( 'RapierPhysics: Unsupported geometry type:', geometry.type );

	return null;

}

async function RapierPhysics() {

	if ( ! RAPIER ) {

		if ( ! initPromise ) {

			initPromise = ( async () => {

				RAPIER = await import( '@dimforge/rapier3d-compat' );
				// Suppress compat package's own deprecation warning (base64 WASM triggers it)
				const origWarn = console.warn;
				console.warn = () => {};
				await RAPIER.init();
				console.warn = origWarn;

			} )();

		}

		await initPromise;

	}

	const gravity = new Vector3( 0.0, - 9.81, 0.0 );
	const world = new RAPIER.World( gravity );

	const meshes = [];
	const meshMap = new WeakMap();

	const _vector = new Vector3();
	const _quaternion = new Quaternion();
	const _matrix = new Matrix4();

	function addScene( scene ) {

		scene.traverse( function ( child ) {

			if ( child.isMesh ) {

				const physics = child.userData.physics;

				if ( physics ) {

					addMesh( child, physics.mass, physics.restitution, physics.friction );

				}

			}

		} );

	}

	function addMesh( mesh, mass = 0, restitution = 0, friction ) {

		// Support collider type override via userData.physics.colliderRadius
		const physicsData = mesh.userData.physics || {};
		const shape = physicsData.colliderRadius
			? RAPIER.ColliderDesc.ball( physicsData.colliderRadius )
			: getShape( mesh.geometry );

		if ( shape === null ) return;

		shape.setMass( mass );
		shape.setRestitution( restitution );
		if ( friction !== undefined ) shape.setFriction( friction );

		const { body, collider } = mesh.isInstancedMesh
			? createInstancedBody( mesh, mass, shape )
			: createBody( mesh.position, mesh.quaternion, mass, shape );

		if ( ! mesh.userData.physics ) mesh.userData.physics = {};

		mesh.userData.physics.body = body;
		mesh.userData.physics.collider = collider;

		if ( mass > 0 ) {

			meshes.push( mesh );
			meshMap.set( mesh, { body, collider } );

		}

	}

	function removeMesh( mesh ) {

		const index = meshes.indexOf( mesh );

		if ( index !== - 1 ) {

			meshes.splice( index, 1 );
			meshMap.delete( mesh );

			if ( ! mesh.userData.physics ) return;

			const body = mesh.userData.physics.body;
			const collider = mesh.userData.physics.collider;

			if ( body ) removeBody( body );
			if ( collider ) removeCollider( collider );

		}

	}

	function createInstancedBody( mesh, mass, shape ) {

		const array = mesh.instanceMatrix.array;

		const bodies = [];
		const colliders = [];

		for ( let i = 0; i < mesh.count; i ++ ) {

			const position = _vector.fromArray( array, i * 16 + 12 );
			const { body, collider } = createBody( position, null, mass, shape );
			bodies.push( body );
			colliders.push( collider );

		}

		return { body: bodies, collider: colliders };

	}

	function createBody( position, quaternion, mass, shape ) {

		const desc = mass > 0 ? RAPIER.RigidBodyDesc.dynamic() : RAPIER.RigidBodyDesc.fixed();
		desc.setTranslation( ...position );
		if ( quaternion !== null ) desc.setRotation( quaternion );

		const body = world.createRigidBody( desc );
		const collider = world.createCollider( shape, body );

		return { body, collider };

	}

	function removeBody( body ) {

		if ( Array.isArray( body ) ) {

			for ( let i = 0; i < body.length; i ++ ) {

				world.removeRigidBody( body[ i ] );

			}

		} else {

			world.removeRigidBody( body );

		}

	}

	function removeCollider( collider ) {

		if ( Array.isArray( collider ) ) {

			for ( let i = 0; i < collider.length; i ++ ) {

				world.removeCollider( collider[ i ] );

			}

		} else {

			world.removeCollider( collider );

		}

	}

	function setMeshPosition( mesh, position, index = 0 ) {

		const data = meshMap.get( mesh );
		if ( ! data ) return;

		let body = data.body;

		if ( mesh.isInstancedMesh ) {

			body = body[ index ];

		}

		body.setAngvel( ZERO );
		body.setLinvel( ZERO );
		body.setTranslation( position );
		body.wakeUp();

	}

	function setMeshVelocity( mesh, velocity, index = 0 ) {

		const data = meshMap.get( mesh );
		if ( ! data ) return;

		let body = data.body;

		if ( mesh.isInstancedMesh ) {

			body = body[ index ];

		}

		body.setLinvel( velocity );
		body.wakeUp();

	}

	//

	const timer = new Timer();

	function step() {

		if ( disposed ) return;

		timer.update();

		world.timestep = timer.getDelta();
		world.step();

		//

		for ( let i = 0, l = meshes.length; i < l; i ++ ) {

			const mesh = meshes[ i ];

			if ( mesh.isInstancedMesh ) {

				const array = mesh.instanceMatrix.array;
				const { body: bodies } = meshMap.get( mesh );

				for ( let j = 0; j < bodies.length; j ++ ) {

					const body = bodies[ j ];

					const position = body.translation();
					_quaternion.copy( body.rotation() );

					_matrix.compose( position, _quaternion, _scale ).toArray( array, j * 16 );

				}

				mesh.instanceMatrix.needsUpdate = true;

			} else {

				const { body } = meshMap.get( mesh );

				mesh.position.copy( body.translation() );
				mesh.quaternion.copy( body.rotation() );

			}

		}

	}

	// animate

	let disposed = false;

	function dispose() {

		disposed = true;
		meshes.length = 0;
		world.free();

	}

	return {
		RAPIER,
		world,
		addScene,
		addMesh,
		removeMesh,
		setMeshPosition,
		setMeshVelocity,
		step,
		dispose,
	};

}

export { RapierPhysics };
