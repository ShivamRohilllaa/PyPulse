/* The posted JSON is the sole source of truth; without JavaScript it stays directly editable. */
(() => {
  const raw = document.getElementById("id_steps");
  if (!raw) return;
  const rawBlock = raw.parentElement;
  const make = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const button = (text, action) => {
    const b = make("button", text);
    b.type = "button";
    b.addEventListener("click", action);
    return b;
  };
  const editor = make("div", undefined, "scenario-editor");
  const heading = make("h3", "Request steps");
  const cards = make("div");
  const status = make("p", "", "muted");
  status.setAttribute("aria-live", "polite");
  rawBlock.before(editor);
  const rawDetails = make("details");
  rawDetails.append(make("summary", "Edit scenario as JSON"));
  rawBlock.before(rawDetails);
  rawDetails.append(rawBlock);
  let steps = [];
  let valid = true;
  const sync = () => {
    raw.value = JSON.stringify(steps, null, 2);
    raw.setCustomValidity("");
  };
  const choose = (step, key, value) => {
    if (value === "" || value === undefined) delete step[key];
    else step[key] = value;
    sync();
  };
  function field(parent, label, value, onChange, options = {}) {
    const wrap = make("label", label);
    const input = make(options.choices ? "select" : options.rows ? "textarea" : "input");
    if (options.choices) options.choices.forEach((c) => input.add(new Option(c, c)));
    else if (options.rows) input.rows = options.rows;
    else input.type = "text";
    input.value = value ?? "";
    if (options.placeholder) input.placeholder = options.placeholder;
    input.addEventListener("input", () => onChange(input.value, input));
    wrap.append(input); parent.append(wrap);
    return input;
  }
  function render(focusIndex) {
    cards.replaceChildren();
    if (!valid) {
      status.textContent = "The JSON must be a list of request objects before you can use the step editor.";
      return;
    }
    status.textContent = steps.length ? steps.length + (steps.length === 1 ? " step" : " steps") : "No steps: PyPulse will send GET to the default endpoint.";
    const focus = [];
    steps.forEach((step, index) => {
      const box = make("fieldset", undefined, "scenario-step");
      const legend = make("legend", "Step " + (index + 1));
      box.append(legend);
      const main = make("div", undefined, "step-grid");
      box.append(main);
      focus.push(field(main, "Step name", step.name, v => choose(step, "name", v), {placeholder: "Find order"}));
      field(main, "Method", step.method || "GET", v => choose(step, "method", v), {choices: ["GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"]});
      field(box, "Endpoint URL", step.url, v => choose(step,"url",v), {placeholder: "Use default endpoint, or enter a URL"});
      field(box, "Expected HTTP statuses", step.expected_statuses ?? step.expected_status, v => {
        delete step.expected_status; choose(step,"expected_statuses",v);
      }, {placeholder: "Use test default, or enter 200,201,204"});
      window.PyPulseRequest.mount(box,step,(key,value)=>choose(step,key,value===null ? undefined : value),{variableAuth:true});
      const extraction = make("div", undefined, "step-extract");
      extraction.append(make("b", "Extract response values"));
      const captures = Object.entries(step.extract || {}).map(([name, path]) => ({name, path}));
      const redrawExtract = () => {
        extraction.querySelectorAll(".extract-row").forEach(e => e.remove());
        captures.forEach((capture, j) => {
          const row = make("div", undefined, "step-grid extract-row");
          const change = () => {
            const populated = captures.filter(c => c.name && c.path);
            choose(step, "extract", populated.length ? Object.fromEntries(populated.map(c => [c.name,c.path])) : undefined);
          };
          const n = field(row, "Variable name",capture.name,v=>{capture.name=v;check();}, {placeholder:"item_id"});
          const p = field(row, "JSON path, header or regex",capture.path,v=>{capture.path=v;check();}, {placeholder:"json:item.id"});
          const check = () => {
            n.setCustomValidity(capture.path && !capture.name ? "Enter a variable name." : "");
            p.setCustomValidity(capture.name && !capture.path ? "Enter json:field.path, header:Header-Name or regex:pattern." : "");
            change();
          };
          row.append(button("Remove extraction", () => { captures.splice(j,1);change();redrawExtract(); }));
          extraction.insertBefore(row,addCapture);
        });
      };
      const addCapture = button("＋ Extract a value", () => {
        captures.push({name:"",path:""});redrawExtract();
        extraction.querySelectorAll(".extract-row")[captures.length-1].querySelector("input").focus();
      });
      extraction.append(addCapture);box.append(extraction);redrawExtract();
      const actions = make("div", undefined, "actions");
      function move(to) {
        [steps[index],steps[to]]=[steps[to],steps[index]];sync();render(to);
      }
      const up = button("Move up", () => move(index-1));
      const down = button("Move down", () => move(index+1));
      up.disabled=!index;down.disabled=index===steps.length-1;
      const remove=button("Remove step",()=>{steps.splice(index,1);sync();render(Math.min(index,steps.length-1));if(!steps.length)add.focus();});
      actions.append(up,down,remove);box.append(actions);cards.append(box);
    });
    if (focusIndex !== undefined && focus[focusIndex]) focus[focusIndex].focus();
    add.disabled = steps.length>=20;
  }
  function importRaw() {
    try {
      const value=JSON.parse(raw.value || "[]") ?? [];
      if (!Array.isArray(value) || !value.every(v => v && typeof v==="object" && !Array.isArray(v))) throw new Error();
      steps=value;valid=true;raw.setCustomValidity("");add.disabled=false;
    } catch {
      valid=false;raw.setCustomValidity("Enter a JSON array of step objects.");rawDetails.open=true;add.disabled=true;
    }
    render();
  }
  const add=button("＋ Add step",()=>{
    if (!valid || steps.length>=20) return;
    steps.push({method:"GET"});sync();render(steps.length-1);
  });
  editor.append(heading,cards,add,status);
  raw.addEventListener("input",importRaw);
  importRaw();
})();
