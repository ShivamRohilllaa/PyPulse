(() => {
  const raw = document.getElementById("id_options");
  const patch = document.getElementById("id_options_patch");
  if (!raw || !patch) return;
  const block = raw.closest("p");
  const editor = document.createElement("section");
  const heading = document.createElement("h2");
  heading.textContent = "Request configuration";
  editor.append(heading);
  block.before(editor);
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "Advanced JSON";
  details.append(summary);
  block.before(details);
  details.append(block);
  let changes = {};
  window.PyPulseRequest.mount(editor,{},(key,value)=>{
    if (raw.value && patch.value !== "1") {
      try { changes=JSON.parse(raw.value); }
      catch { raw.setCustomValidity("Enter valid request JSON or clear it before using the editor.");details.open=true;return; }
    } else { patch.value="1"; }
    changes[key]=value;
    if (patch.value !== "1" && value === null) delete changes[key];
    raw.value=JSON.stringify(changes);raw.setCustomValidity("");
  },{privateEdit:editor.closest("form").dataset.saved==="yes",assertions:true});
  raw.addEventListener("input",()=>{ patch.value="";changes={};raw.setCustomValidity(""); });
})();
