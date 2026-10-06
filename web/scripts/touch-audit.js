// Paste into Chrome's console with device emulation enabled. No app imports.
(() => {
  const interactive =
    'a[href], button, [role="button"], [role="option"], [role="menuitem"], input, select, textarea, summary';
  const radius = 12;
  const failures = [];
  const targets = [];

  function path(element) {
    const parts = [];
    for (let node = element; node && node !== document.body; node = node.parentElement) {
      let part = node.localName;
      if (node.id) {
        parts.unshift(`${part}#${CSS.escape(node.id)}`);
        break;
      }
      part += [...node.classList].map((name) => `.${CSS.escape(name)}`).join('');
      if (node.parentElement) {
        part += `:nth-child(${[...node.parentElement.children].indexOf(node) + 1})`;
      }
      parts.unshift(part);
    }
    return parts.join(' > ');
  }

  function rect(left, top, right, bottom) {
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  }

  function pixels(value, basis) {
    return value.endsWith('%') ? (parseFloat(value) * basis) / 100 : parseFloat(value);
  }

  function hitRect(element) {
    const box = element.getBoundingClientRect();
    let hit = rect(box.left, box.top, box.right, box.bottom);
    const before = getComputedStyle(element, '::before');
    if (!['none', 'normal', ''].includes(before.content) && before.position === 'absolute') {
      // Absolute pseudo-elements use the originating element's padding box
      // when it is positioned, otherwise the nearest positioned ancestor.
      let container = element;
      while (container && getComputedStyle(container).position === 'static') {
        container = container.parentElement;
      }
      const bounds = container?.getBoundingClientRect();
      const originX = bounds ? bounds.left + container.clientLeft : -scrollX;
      const originY = bounds ? bounds.top + container.clientTop : -scrollY;
      const width = container ? container.clientWidth : innerWidth;
      const height = container ? container.clientHeight : innerHeight;
      let w = pixels(before.width, width);
      let h = pixels(before.height, height);
      if (before.boxSizing !== 'border-box') {
        w += parseFloat(before.paddingLeft) + parseFloat(before.paddingRight);
        w += parseFloat(before.borderLeftWidth) + parseFloat(before.borderRightWidth);
        h += parseFloat(before.paddingTop) + parseFloat(before.paddingBottom);
        h += parseFloat(before.borderTopWidth) + parseFloat(before.borderBottomWidth);
      }
      let x = pixels(before.left, width);
      let y = pixels(before.top, height);
      if (!Number.isFinite(x)) x = width - pixels(before.right, width) - w;
      if (!Number.isFinite(y)) y = height - pixels(before.bottom, height) - h;
      x += parseFloat(before.marginLeft);
      y += parseFloat(before.marginTop);
      if ([x, y, w, h].every(Number.isFinite)) {
        const matrix = new DOMMatrix(before.transform === 'none' ? undefined : before.transform);
        const [ox, oy] = before.transformOrigin.split(' ').map(parseFloat);
        const corners = [
          [0, 0],
          [w, 0],
          [0, h],
          [w, h],
        ].map(([cx, cy]) => {
          const point = new DOMPoint(cx - ox, cy - oy).matrixTransform(matrix);
          return { x: originX + x + ox + point.x, y: originY + y + oy + point.y };
        });
        hit = rect(
          Math.min(hit.left, ...corners.map((p) => p.x)),
          Math.min(hit.top, ...corners.map((p) => p.y)),
          Math.max(hit.right, ...corners.map((p) => p.x)),
          Math.max(hit.bottom, ...corners.map((p) => p.y)),
        );
      }
    }
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      const bounds = parent.getBoundingClientRect();
      const left = bounds.left + parent.clientLeft;
      const top = bounds.top + parent.clientTop;
      if (style.overflowX !== 'visible') {
        hit.left = Math.max(hit.left, left);
        hit.right = Math.min(hit.right, left + parent.clientWidth);
      }
      if (style.overflowY !== 'visible') {
        hit.top = Math.max(hit.top, top);
        hit.bottom = Math.min(hit.bottom, top + parent.clientHeight);
      }
    }
    return rect(hit.left, hit.top, hit.right, hit.bottom);
  }

  function inlineLink(element) {
    if (!element.matches('a[href]') || getComputedStyle(element).display !== 'inline') return false;
    const textBlock = element.closest('p, li, dd, dt, blockquote, .prose');
    if (!textBlock) return false;
    // A standalone link in a list is not running text.
    const copy = textBlock.cloneNode(true);
    for (const link of copy.querySelectorAll(interactive)) link.remove();
    return !!copy.textContent.trim();
  }

  for (const element of document.querySelectorAll(interactive)) {
    const style = getComputedStyle(element);
    if (
      element.matches(':disabled') ||
      element.closest('[hidden], [inert], [aria-disabled="true"]') ||
      style.visibility !== 'visible' ||
      style.display === 'none' ||
      inlineLink(element)
    )
      continue;
    const box = element.getBoundingClientRect();
    if (!box.width || !box.height) continue;
    const hit = hitRect(element);
    if (
      hit.width <= 0 ||
      hit.height <= 0 ||
      hit.right <= 0 ||
      hit.bottom <= 0 ||
      hit.left >= innerWidth ||
      hit.top >= innerHeight
    )
      continue;
    targets.push({
      element,
      hit,
      selector: path(element),
      small: hit.width < 24 || hit.height < 24,
    });
  }

  function circleIntersects(circle, box) {
    const x = Math.max(box.left, Math.min(circle.x, box.right));
    const y = Math.max(box.top, Math.min(circle.y, box.bottom));
    return Math.hypot(circle.x - x, circle.y - y) < radius;
  }

  for (const target of targets) {
    const { element, hit, selector, small } = target;
    const size = `${hit.width.toFixed(1)} × ${hit.height.toFixed(1)}`;
    if (small) {
      const centre = { x: (hit.left + hit.right) / 2, y: (hit.top + hit.bottom) / 2 };
      const neighbour = targets.find((other) => {
        if (other === target) return false;
        if (circleIntersects(centre, other.hit)) return true;
        return (
          other.small &&
          Math.hypot(
            centre.x - (other.hit.left + other.hit.right) / 2,
            centre.y - (other.hit.top + other.hit.bottom) / 2,
          ) <
            2 * radius
        );
      });
      if (neighbour)
        failures.push({ selector, size, reason: 'floor / spacing', other: neighbour.selector });
    }
    const coveredBy = new Map();
    for (let y = Math.max(0, hit.top) + 0.5; y < Math.min(innerHeight, hit.bottom); y += 4) {
      for (let x = Math.max(0, hit.left) + 0.5; x < Math.min(innerWidth, hit.right); x += 4) {
        const winner = document.elementFromPoint(x, y)?.closest(interactive);
        if (winner && winner !== element && !coveredBy.has(winner)) coveredBy.set(winner, { x, y });
      }
    }
    for (const [winner, point] of coveredBy) {
      failures.push({ selector, size, reason: 'overlap', other: path(winner), point });
    }
  }
  console.info(
    `Touch audit: ${targets.length} targets; coarse pointer: ${matchMedia('(pointer: coarse)').matches}`,
  );
  console.table(failures);
  // Keep geometry available for before/after comparisons without rerunning layout.
  window.touchAudit = {
    viewport: { width: innerWidth, height: innerHeight },
    coarse: matchMedia('(pointer: coarse)').matches,
    targets: targets.map(({ selector, hit, small }) => ({ selector, hit, small })),
    failures,
  };
  return failures;
})();
