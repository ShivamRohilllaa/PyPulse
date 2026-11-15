const reports = {
  traffic: {title:"Find the failing route.", description:"Thirty requests hit a Django orders app. Six were deliberately slow. Three failed. Route-level counts show exactly which view returned each error.", alt:"Django traffic report showing 30 requests and three intentional errors."},
  benchmark: {title:"Measure a release under load.", description:"All 300 requests to the order lookup succeeded in this separate local run. The measured p95 was 8.62 ms, p99 was 25.65 ms, and the worker dropped zero iterations.", alt:"Completed Python benchmark with 300 HTTP 200 responses and no dropped iterations."}
};
for (const button of document.querySelectorAll("[data-report]")) {
  button.addEventListener("click", () => {
    const name = button.dataset.report;
    const data = reports[name];
    for (const option of document.querySelectorAll("[data-report]")) option.setAttribute("aria-pressed", String(option === button));
    document.querySelector("#report-description h3").textContent = data.title;
    document.querySelector("#report-description p").textContent = data.description;
    const src = "screenshots/"+name+".jpg";
    const image = document.querySelector("#screen-img");
    image.src=src; image.alt=data.alt; image.height=name==="traffic"?1220:944;
    for (const id of ["#screen-link","#full-image"]) document.querySelector(id).href=src;
  });
}
const django = document.querySelector("#snippet code").textContent;
const frameworks = {
  Django:[django,"Install the agent into your app environment, set its URL and token, then add this line after your existing Django middleware definition."],
  DRF:[django,"Add the middleware once in Django settings. DRF APIViews, viewsets and serializers use the same request pipeline."],
  Flask:["from flask import Flask\nfrom pypulse_agent.flask import instrument\n\napp = Flask(__name__)\ninstrument(app)","Call instrument once while constructing the Flask app. The repository includes a runnable order endpoint."],
  FastAPI:["from fastapi import FastAPI\nfrom pypulse_agent.fastapi import PulseMiddleware\n\napp = FastAPI()\napp.add_middleware(PulseMiddleware)","Register the adapter on the instance served by Uvicorn. Timing includes the final ASGI response body."]
};
document.querySelector("#framework").addEventListener("change",event=>{
 const [snippet,note]=frameworks[event.target.value];
 document.querySelector("#snippet code").textContent=snippet;
 document.querySelector("#install-note").textContent=note;
});
for(const button of document.querySelectorAll("[data-copy]")){
 button.addEventListener("click",async()=>{
  const block=document.getElementById(button.dataset.copy);
  try { await navigator.clipboard.writeText(block.textContent); button.textContent="Copied ✓"; }
  catch {const selection=window.getSelection(); const range=document.createRange(); range.selectNodeContents(block); selection.removeAllRanges(); selection.addRange(range); button.textContent="Text selected";}
  setTimeout(()=>{button.textContent="Copy";},1800);
 });
}
