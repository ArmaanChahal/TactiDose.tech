/**
 * SVG diagram of the medication carousel (handoff §5): N numbered sectors
 * (compartment k = protocol slot k-1, numbered clockwise from home), a fixed
 * access gate at the top, and the sector currently at the gate highlighted.
 *
 * The disk rotates so the slot at the gate sits under the gate marker, like the
 * real device: rotation `angleDeg` means the sector centred at that angle (slot
 * k is centred at k × 360/N) is at the gate. Numbers stay upright.
 * Geometry helpers are pure and unit-tested; rendering uses createElementNS only.
 */

import { s, uid } from './dom.js';

export const R_OUT = 112;
export const R_IN = 42;
const R_LABEL = 80;

const round = (v) => Math.round(v * 100) / 100;

/** Point at radius r and angle deg (0 = 12 o'clock, clockwise). */
export function polar(r, deg) {
  const a = (deg * Math.PI) / 180;
  return [round(r * Math.sin(a)), round(-r * Math.cos(a))];
}

export function slotAngle(slot, numSlots) {
  return (slot * 360) / numSlots;
}

/** Signed shortest rotation from `from` to `to` degrees, in (-180, 180]. */
export function shortestDelta(from, to) {
  const d = ((((to - from) % 360) + 540) % 360) - 180;
  return d === -180 ? 180 : d;
}

/** Path for the annulus sector of `slot`, centred on its slot angle. */
export function sectorPath(slot, numSlots, rOut = R_OUT, rIn = R_IN) {
  const span = 360 / numSlots;
  const a0 = slotAngle(slot, numSlots) - span / 2;
  const a1 = a0 + span;
  const large = span > 180 ? 1 : 0;
  const [x0, y0] = polar(rOut, a0);
  const [x1, y1] = polar(rOut, a1);
  const [x2, y2] = polar(rIn, a1);
  const [x3, y3] = polar(rIn, a0);
  return `M${x0} ${y0}A${rOut} ${rOut} 0 ${large} 1 ${x1} ${y1}L${x2} ${y2}A${rIn} ${rIn} 0 ${large} 0 ${x3} ${y3}Z`;
}

function prefersReducedMotion() {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

export class CarouselView {
  /**
   * @param {HTMLElement} container element the SVG is appended to
   * @param {{numSlots?: number, label?: string}} [options]
   */
  constructor(container, { numSlots = 6, label = 'Carousel diagram' } = {}) {
    this.container = container;
    this.label = label;
    this.angle = 0;
    this.target = 0;
    this.raf = null;
    this.state = { slot: null, gate: 'UNKNOWN', targetSlot: null, assigned: new Set() };
    this.build(numSlots);
  }

  build(numSlots) {
    this.n = Math.max(2, Number(numSlots) || 6);
    const titleId = uid('carousel-title');
    const descId = uid('carousel-desc');
    this.desc = s('desc', { id: descId });
    this.disk = s('g', { class: 'c-disk' });
    this.sectors = [];
    this.labels = [];
    this.nums = [];
    this.pips = [];
    for (let i = 0; i < this.n; i += 1) {
      const sector = s('path', { class: 'c-sector', d: sectorPath(i, this.n) });
      const [x, y] = polar(R_LABEL, slotAngle(i, this.n));
      const num = s('text', { class: 'c-num', x: 0, y: -6, text: String(i + 1) });
      const pip = s('circle', { class: 'c-pip', cx: 0, cy: 17, r: 0 });
      const label = s('g', { class: 'c-label', 'data-x': x, 'data-y': y }, num, pip);
      this.sectors.push(sector);
      this.labels.push(label);
      this.nums.push(num);
      this.pips.push(pip);
      this.disk.append(sector);
    }
    for (const label of this.labels) this.disk.append(label);
    this.disk.append(s('circle', { class: 'c-hub', r: R_IN - 8 }));

    const gateY = -R_OUT - 32;
    this.gateBar = s('rect', { class: 'c-gate-bar', x: -32, y: gateY + 3, width: 64, height: 16, rx: 3 });
    this.gateText = s('text', { class: 'c-gate-text', x: 0, y: gateY - 10, text: 'GATE' });
    const gate = s('g', { class: 'c-gate' },
      this.gateText,
      s('rect', { class: 'c-gate-frame', x: -38, y: gateY - 2, width: 76, height: 26, rx: 5 }),
      this.gateBar,
      s('path', { class: 'c-pointer', d: `M-9 ${-R_OUT - 5}L9 ${-R_OUT - 5}L0 ${-R_OUT + 8}Z` }),
    );

    this.svg = s('svg', {
      class: 'carousel',
      viewBox: `-128 ${-R_OUT - 78} 256 ${R_OUT * 2 + 88}`,
      role: 'img',
      'aria-labelledby': `${titleId} ${descId}`,
    }, s('title', { id: titleId, text: this.label }), this.desc, gate, this.disk);
    this.container.replaceChildren(this.svg);
    this.applyRotation();
    this.render();
  }

  setNumSlots(numSlots) {
    if (Number(numSlots) && Number(numSlots) !== this.n) this.build(numSlots);
  }

  /**
   * @param {{slot?: number|null, angleDeg?: number|null, gate?: string, targetSlot?: number|null,
   *          assignedSlots?: Iterable<number>|null}} update
   */
  update({ slot, angleDeg = null, gate, targetSlot, assignedSlots } = {}) {
    if (slot !== undefined) this.state.slot = slot === null ? null : Number(slot);
    if (gate !== undefined) this.state.gate = gate || 'UNKNOWN';
    if (targetSlot !== undefined) this.state.targetSlot = targetSlot === null ? null : Number(targetSlot);
    if (assignedSlots) this.state.assigned = new Set(Array.from(assignedSlots, Number));
    let angle = null;
    if (angleDeg !== null && angleDeg !== undefined && Number.isFinite(Number(angleDeg))) angle = Number(angleDeg);
    else if (this.state.slot !== null) angle = slotAngle(this.state.slot, this.n);
    if (angle !== null) this.rotateTo(angle);
    this.render();
  }

  rotateTo(angle) {
    this.target = ((angle % 360) + 360) % 360;
    if (prefersReducedMotion() || typeof requestAnimationFrame !== 'function') {
      this.angle = this.target;
      this.applyRotation();
      return;
    }
    if (!this.raf) this.raf = requestAnimationFrame(() => this.step());
  }

  step() {
    const delta = shortestDelta(this.angle, this.target);
    if (Math.abs(delta) < 0.4) {
      this.angle = this.target;
      this.raf = null;
    } else {
      this.angle = (this.angle + delta * 0.2 + 360) % 360;
      this.raf = requestAnimationFrame(() => this.step());
    }
    this.applyRotation();
  }

  applyRotation() {
    this.disk.setAttribute('transform', `rotate(${round(-this.angle)})`);
    for (const label of this.labels) {
      label.setAttribute('transform', `translate(${label.dataset.x} ${label.dataset.y}) rotate(${round(this.angle)})`);
    }
  }

  render() {
    const { slot, gate, targetSlot, assigned } = this.state;
    for (let i = 0; i < this.n; i += 1) {
      const atGate = i === slot;
      this.sectors[i].classList.toggle('is-at-gate', atGate);
      this.sectors[i].classList.toggle('is-target', i === targetSlot && !atGate);
      this.nums[i].classList.toggle('is-at-gate', atGate);
      this.pips[i].classList.toggle('is-at-gate', atGate);
      this.pips[i].setAttribute('r', assigned.has(i) ? '5' : '0');
    }
    const open = gate === 'OPEN';
    const unknown = gate !== 'OPEN' && gate !== 'CLOSED';
    const gateY = -R_OUT - 32;
    this.gateBar.setAttribute('y', String(open ? gateY - 18 : gateY + 3));
    this.gateBar.classList.toggle('is-unknown', unknown);
    this.gateText.textContent = open ? 'GATE OPEN' : unknown ? 'GATE ?' : 'GATE CLOSED';
    this.gateText.setAttribute('y', String(open ? gateY - 26 : gateY - 10));
    this.desc.textContent = this.describe();
  }

  /** Text alternative, also used as the visible caption. */
  describe() {
    const { slot, gate, targetSlot } = this.state;
    const parts = [];
    if (slot !== null && slot !== undefined) parts.push(`Compartment ${slot + 1} is at the gate.`);
    else parts.push('Position unknown (between compartments or not homed).');
    if (targetSlot !== null && targetSlot !== undefined && targetSlot !== slot) parts.push(`Moving to compartment ${targetSlot + 1}.`);
    parts.push(gate === 'OPEN' ? 'Gate open.' : gate === 'CLOSED' ? 'Gate closed.' : 'Gate state unknown.');
    return parts.join(' ');
  }
}
