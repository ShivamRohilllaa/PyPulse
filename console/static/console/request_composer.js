/* Shared monitor and benchmark request editor. No stored monitor credentials cross this boundary. */
(() => {
  function element(tag, text) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    return node;
  }
  function button(label, action) {
    const node = element("button", label);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  }
  function input(parent, label, initial, changed, config = {}) {
    const wrap = element("label", label);
    const control = element(config.choices ? "select" : config.multiline ? "textarea" : "input");
    if (config.choices) {
      for (const [key, title] of Object.entries(config.choices)) control.add(new Option(title, key));
    } else if (!config.multiline) {
      control.type = config.private ? "password" : "text";
      control.autocomplete = config.private ? "new-password" : "off";
    } else {
      control.rows = 3;
    }
    control.value = initial ?? "";
    if (config.placeholder) control.placeholder = config.placeholder;
    control.addEventListener("input", () => changed(control.value, control));
    wrap.append(control);
    parent.append(wrap);
    return control;
  }
  function section(parent, title) {
    const node = element("details");
    node.append(element("summary", title));
    parent.append(node);
    return node;
  }
  function pairs(parent, title, existing, changed) {
    const list = element("div");
    const rows = Object.entries(existing || {}).map(([key, value]) => ({key, value}));
    parent.append(list);
    const sync = () => changed(Object.fromEntries(rows.filter(r => r.key).map(r => [r.key,r.value])));
    function draw() {
      list.replaceChildren();
      rows.forEach((item, index) => {
        const row = element("div");
        row.className = "composer-row";
        const keyInput = input(row, "Name", item.key, value => { item.key = value; sync(); validate(); });
        input(row, "Value", item.value, value => { item.value = value; sync(); validate(); }, {private:true});
        function validate() { keyInput.setCustomValidity(item.value && !item.key ? "Enter a name." : ""); }
        row.append(button("Remove", () => { rows.splice(index,1); draw(); sync(); }));
        list.append(row);
      });
    }
    draw();
    parent.append(button("Add " + title, () => { rows.push({key:"",value:""}); draw(); list.lastChild.querySelector("input").focus(); }));
  }
  function mount(parent, initial, change, settings={}) {
    const root = element("div");
    root.className = "request-composer";
    parent.append(root);
    function write(key, value) { change(key,value); }
    for (const [key,title,name] of [["headers","Headers","header"],["query","Query parameters","parameter"]]) {
      const block = section(root,title);
      pairs(block,name,initial[key],obj => write(key,Object.keys(obj).length ? obj : null));
      if (settings.privateEdit) block.append(button("Clear saved " + title.toLowerCase(), () => write(key,null)));
    }
    const authBlock = section(root,"Authentication");
    const authFields = element("div");
    let auth = {...initial.auth};
    function showAuth(kind) {
      authFields.replaceChildren();
      const fields = {bearer:[["token","Bearer token"]],basic:[["username","Username"],["password","Password"]],
        api_key:[["name","Key name"],["value","Key value"],["in","Send in"]]};
      for (const [key,label] of fields[kind] || []) {
        input(authFields,label,auth[key] || (key==="in" ? "header" : ""), value => {auth[key]=value;write("auth",{...auth});},
          key==="in" ? {choices:{header:"Header",query:"Query parameter"}} :
          {private:key!=="name" && key!=="username",placeholder:settings.variableAuth && ["value","password","token"].includes(key) ? "{{SECRET_NAME}}" : ""});
      }
    }
    const choices = {none:"None",bearer:"Bearer token",basic:"Basic credentials",api_key:"API key"};
    const keep = settings.privateEdit ? {"":"Keep saved authentication",...choices} : choices;
    input(authBlock,"Authentication type",auth.type ?? (settings.privateEdit ? "" : "none"), kind => {
      auth = {type:kind}; showAuth(kind);
      if (kind) write("auth",kind==="none" ? null : {...auth});
    },{choices:keep});
    authBlock.append(authFields);
    showAuth(auth.type);
    const bodyBlock = section(root,"Request body");
    const bodyFields = element("div");
    let kind = initial.form != null ? "form" : initial.body && typeof initial.body === "object" ? "json" :
      initial.body != null && initial.body !== "" ? "text" : settings.privateEdit ? "" : "none";
    function showBody(mode, value) {
      bodyFields.replaceChildren();
      if (mode==="form") pairs(bodyFields,"form field",value,obj=>write("form",obj));
      if (mode==="text") input(bodyFields,"Text body",value,v=>write("body",v),{multiline:true});
      if (mode==="json") input(bodyFields,"JSON object or array",value == null ? "" : JSON.stringify(value,null,2),(v,node)=>{
        try {
          const body=JSON.parse(v);
          if (!body || typeof body!=="object") throw new Error();
          node.setCustomValidity("");write("body",body);
        } catch { node.setCustomValidity("Enter a JSON object or array."); }
      },{multiline:true});
    }
    let formats = {none:"None",text:"Plain text",json:"JSON",form:"Form URL-encoded"};
    if (settings.privateEdit) formats={"":"Keep saved body",...formats};
    input(bodyBlock,"Body format",kind, value=>{
      kind=value; showBody(kind,null);
      if (kind) {write("body",null);write("form",null);}
    },{choices:formats});
    bodyBlock.append(bodyFields);
    showBody(kind,kind==="form" ? initial.form : initial.body);
    const tls = section(root,"TLS verification");
    const tlsChoices = {verify:"Verify certificate",skip:"Allow a self-signed certificate"};
    input(tls,"Server certificate",initial.ignore_tls ? "skip" : settings.privateEdit ? "" : "verify",
      choice=>{if(choice)write("ignore_tls",choice==="skip");},{choices:settings.privateEdit ? {"":"Keep saved setting",...tlsChoices} : tlsChoices});
    if (settings.assertions) {
      const rules = section(root,"JSON response assertions");
      const rows = [];
      const items = element("div");
      rules.append(items);
      function syncRules() {write("json_checks",rows.map(row=>({...row})));}
      function drawRules() {
        items.replaceChildren();
        rows.forEach((row,index)=>{
          const fieldset = element("fieldset");
          fieldset.append(element("legend","Assertion " + (index+1)));
          input(fieldset,"JSON path",row.path,v=>{row.path=v;syncRules();},{placeholder:"items[0].ready"});
          input(fieldset,"Comparison",row.op,v=>{row.op=v;syncRules();drawRules();},
            {choices:{equals:"Equals",not_equals:"Does not equal",contains:"Contains",exists:"Exists"}});
          if (row.op!=="exists") input(fieldset,"Expected text",row.value,v=>{row.value=v;syncRules();},{private:true});
          fieldset.append(button("Remove",()=>{rows.splice(index,1);drawRules();syncRules();}));
          items.append(fieldset);
        });
      }
      rules.append(button("Add assertion",()=>{rows.push({path:"",op:"exists",value:""});drawRules();syncRules();}));
      if(settings.privateEdit)rules.append(button("Clear saved assertions",()=>{rows.length=0;drawRules();syncRules();}));
    }
  }
  window.PyPulseRequest = {mount,input,pairs};
})();
