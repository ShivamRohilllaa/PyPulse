(() => {
  const raw = document.querySelector("#id_configuration");
  const patch = document.querySelector("#id_config_patch");
  const kind = document.querySelector("#id_kind");
  const block = raw.closest("p");
  const container = document.createElement("div");
  container.className="request-composer";
  block.before(container);
  const advanced = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "Advanced JSON";
  advanced.append(summary);block.before(advanced);advanced.append(block);
  const original = kind.value;
  const isEdit = raw.form.dataset.saved === "yes";
  let changes = {};
  function write(key,value) {
    if(raw.value && !patch.value && Object.keys(changes).length === 0){
      try {changes=JSON.parse(raw.value);}
      catch {raw.setCustomValidity("Enter valid configuration JSON or clear it before using these fields.");advanced.open=true;return;}
    }
    changes[key]=value;
    patch.value=isEdit && kind.value===original ? "1" : "";
    raw.value=JSON.stringify(changes);raw.setCustomValidity("");
  }
  function render() {
    container.replaceChildren();
    const fields=window.PyPulseRequest;
    const add=(key,label,privateValue=false,transform=v=>v)=>fields.input(container,label,"",value=>write(key,transform(value)),{private:privateValue});
    if(kind.value==="email"){
      add("host","SMTP hostname");add("port","Port",false,Number);
      fields.input(container,"Connection security","",v=>{if(v)write("security",v);},{choices:{"":"Default or saved setting",starttls:"STARTTLS",tls:"Implicit TLS",plain:"Unencrypted relay"}});
      add("from","Sender address");add("to","Recipient addresses (comma-separated)",false,v=>v.split(",").map(s=>s.trim()).filter(Boolean));
      add("username","SMTP username");add("password","SMTP password",true);
    } else {
      add("url","Webhook URL",true);
      if(kind.value==="slack")add("mention","Mention (optional)");
      if(kind.value==="webhook"){
        add("secret","Signing secret (optional)",true);
        fields.pairs(container,"webhook header",{},obj=>write("headers",obj));
      }
    }
  }
  kind.addEventListener("change",()=>{changes={};patch.value="";raw.value="";render();});
  raw.addEventListener("input",()=>{changes={};patch.value="";raw.setCustomValidity("");});
  render();
})();
