// A minimal DOM for running the page scripts in Node: every element records its listeners, children and classes,
// so a test can fire an event and read what the script did. Not a browser: it checks the script's logic, not its looks.
function installDom() {
  const elements = {};
  const make = (id) => {
    const el = {
      id, textContent: "", title: "", hidden: false, className: "", type: "",
      dataset: {}, style: {}, listeners: {}, children: [], attrs: {}, classes: new Set(),
      clientWidth: 800, clientHeight: 300,
      classList: { toggle(name, on) { if (on) el.classes.add(name); else el.classes.delete(name); } },
      setAttribute(name, value) { el.attrs[name] = String(value); },
      addEventListener(type, fn) { el.listeners[type] = fn; },
      append(...nodes) { el.children.push(...nodes); },
      replaceChildren(...nodes) { el.children = [...nodes]; },
      getContext: () => new Proxy({}, { get: () => () => {}, set: () => true }),
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 300 }),
    };
    return el;
  };
  const document = {
    getElementById: (id) => (elements[id] = elements[id] || make(id)),
    createElement: (tag) => make("<" + tag + ">"),
    querySelectorAll: (selector) => (selector === ".legend-item" ? document.getElementById("legend").children : []),
  };
  // A range input keeps its value, min and max as strings, whatever the script assigns.
  const slider = document.getElementById("current-limit");
  for (const [name, initial] of [["min", "6"], ["max", "16"], ["value", "6"]]) {
    let stored = initial;
    Object.defineProperty(slider, name, { get: () => stored, set: (next) => { stored = String(next); } });
  }
  global.document = document;
  global.window = { devicePixelRatio: 1, addEventListener() {}, setInterval() {} };
  return { elements, document };
}

module.exports = { installDom };
