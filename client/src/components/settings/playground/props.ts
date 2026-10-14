/** 乐园共用道具（plan-playground-v2）：树、石头、云、蘑菇、栅栏等，多个世界复用，统一「软胶微缩」风格。 */
import {
  BufferGeometry, ConeGeometry, CylinderGeometry, Group, Mesh, SphereGeometry, Vector2, LatheGeometry, Vector3,
  type Material,
} from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import { Bag, bladeGeometry, lumpy, shadow } from "./kit";

/** 弯曲的树干：一串渐细圆柱沿小幅弯折堆叠 */
export function trunk(bag: Bag, mat: Material, h: number, r0: number, r1: number, bend: number, r: () => number) {
  const g = new Group();
  const segs = 4;
  let y = 0, x = 0, z = 0;
  for (let i = 0; i < segs; i++) {
    const t0 = i / segs, t1 = (i + 1) / segs;
    const ra = r0 + (r1 - r0) * t0, rb = r0 + (r1 - r0) * t1;
    const sh = h / segs;
    const m = new Mesh(bag.geo(new CylinderGeometry(rb, ra, sh * 1.06, 10)), mat);
    const dx = (r() - 0.5) * bend, dz = (r() - 0.5) * bend;
    m.position.set(x + dx / 2, y + sh / 2, z + dz / 2);
    m.rotation.set(dz / sh * 0.8, 0, -dx / sh * 0.8);
    g.add(m);
    x += dx; z += dz; y += sh;
  }
  return { group: g, top: new Vector3(x, y, z) };
}

/** 针叶树：3~4 层错落的锥 + 树干 */
export function conifer(bag: Bag, leaf: Material, bark: Material, r: () => number, s = 1) {
  const g = new Group();
  const t = new Mesh(bag.geo(new CylinderGeometry(0.12, 0.18, 0.9, 8)), bark);
  t.position.y = 0.45;
  g.add(t);
  const tiers = 3 + (r() > 0.5 ? 1 : 0);
  for (let i = 0; i < tiers; i++) {
    const k = i / tiers;
    const c = new Mesh(bag.geo(new ConeGeometry(1.15 - k * 0.62, 1.25 - k * 0.2, 9, 2)), leaf);
    c.position.y = 0.95 + i * 0.62;
    c.rotation.y = r() * 3;
    c.rotation.z = (r() - 0.5) * 0.08;
    g.add(c);
  }
  g.scale.setScalar(s);
  return shadow(g, true, true);
}

/** 阔叶树：树干 + 团块树冠（几个噪声球叠成一朵） */
export function roundTree(bag: Bag, leaf: Material[], bark: Material, r: () => number, s = 1, crownN = 4) {
  const g = new Group();
  const { group, top } = trunk(bag, bark, 1.3, 0.2, 0.11, 0.35, r);
  g.add(group);
  for (let i = 0; i < crownN; i++) {
    const a = (i / crownN) * Math.PI * 2 + r();
    const rad = i === 0 ? 0 : 0.55 + r() * 0.2;
    const m = new Mesh(bag.geo(lumpy(0.7 + r() * 0.25, 2, 0.32, 1.6, r() * 50)), leaf[i % leaf.length]);
    m.position.set(top.x + Math.cos(a) * rad, top.y + 0.35 + (i === 0 ? 0.45 : r() * 0.3), top.z + Math.sin(a) * rad * 0.8);
    g.add(m);
  }
  g.scale.setScalar(s);
  return shadow(g, true, true);
}

/** 圆润的石头（可带苔藓帽） */
export function rock(bag: Bag, mat: Material, r: () => number, s = 1, moss?: Material) {
  const g = new Group();
  const m = new Mesh(bag.geo(lumpy(1, 2, 0.45, 1.3, r() * 40)), mat);
  m.scale.set(1, 0.62, 0.86);
  m.position.y = 0.3;
  g.add(m);
  if (moss) {
    const c = new Mesh(bag.geo(lumpy(0.82, 2, 0.5, 2.4, r() * 40)), moss);
    c.scale.set(0.95, 0.32, 0.8);
    c.position.y = 0.62;
    g.add(c);
  }
  g.scale.setScalar(s);
  g.rotation.y = r() * 6;
  return shadow(g, true, true);
}

/** 蓬松的云 */
export function cloud(bag: Bag, mat: Material, r: () => number) {
  const g = new Group();
  const n = 5;
  for (let i = 0; i < n; i++) {
    const m = new Mesh(bag.geo(lumpy(1, 2, 0.18, 1.2, r() * 30)), mat);
    const x = (i - (n - 1) / 2) * 0.75;
    const big = 1 - Math.abs(x) / 2.4;
    m.scale.set(0.7 + big * 0.5, 0.48 + big * 0.42, 0.6);
    m.position.set(x, big * 0.32, (r() - 0.5) * 0.3);
    g.add(m);
  }
  return g;
}

/** 蘑菇：车削菌柄 + 半球菌盖 + 白色斑点 */
export function mushroom(bag: Bag, cap: Material, stem: Material, dots: Material | null, r: () => number, s = 1) {
  const g = new Group();
  const pts = [new Vector2(0, 0), new Vector2(0.16, 0), new Vector2(0.13, 0.3), new Vector2(0.11, 0.55), new Vector2(0, 0.56)];
  const st = new Mesh(bag.geo(new LatheGeometry(pts, 16)), stem);
  const cp = new Mesh(bag.geo(new SphereGeometry(0.42, 24, 14, 0, Math.PI * 2, 0, Math.PI * 0.55)), cap);
  cp.scale.y = 0.75;
  cp.position.y = 0.45;
  g.add(st, cp);
  if (dots) {
    const dg = bag.geo(new SphereGeometry(0.06, 10, 6));
    for (let i = 0; i < 6; i++) {
      const th = 0.35 + r() * 0.75, ph = r() * Math.PI * 2;
      const d = new Mesh(dg, dots);
      d.position.set(Math.sin(th) * Math.cos(ph) * 0.42, 0.45 + Math.cos(th) * 0.42 * 0.75, Math.sin(th) * Math.sin(ph) * 0.42);
      d.scale.set(1, 0.4, 1);
      d.lookAt(d.position.clone().multiplyScalar(2).setY(d.position.y * 2 - 0.45));
      g.add(d);
    }
  }
  g.scale.setScalar(s);
  g.rotation.z = (r() - 0.5) * 0.2;
  return shadow(g, true, true);
}

/** 一丛蕨 / 草：多片叶子绕中心外倾，合并成一个几何 */
export function fernGeometry(leaves = 8, h = 0.9, w = 0.2): BufferGeometry {
  const parts: BufferGeometry[] = [];
  for (let i = 0; i < leaves; i++) {
    const b = bladeGeometry(h, w, 5);
    b.rotateX(0.55 + (i % 2) * 0.25);
    b.rotateY((i / leaves) * Math.PI * 2);
    parts.push(b);
  }
  const g = mergeGeometries(parts)!;
  parts.forEach((p) => p.dispose());
  return g;
}

/** 木栅栏一段 */
export function fence(bag: Bag, mat: Material, len: number, posts: number) {
  const g = new Group();
  const pg = bag.geo(new CylinderGeometry(0.07, 0.08, 0.9, 8));
  const cap = bag.geo(new ConeGeometry(0.08, 0.12, 8));
  for (let i = 0; i < posts; i++) {
    const x = -len / 2 + (i / (posts - 1)) * len;
    const p = new Mesh(pg, mat); p.position.set(x, 0.45, 0);
    const c = new Mesh(cap, mat); c.position.set(x, 0.96, 0);
    g.add(p, c);
  }
  const rg = bag.geo(new CylinderGeometry(0.04, 0.04, len, 6));
  for (const y of [0.35, 0.7]) { const rl = new Mesh(rg, mat); rl.rotation.z = Math.PI / 2; rl.position.y = y; g.add(rl); }
  return shadow(g, true, true);
}
