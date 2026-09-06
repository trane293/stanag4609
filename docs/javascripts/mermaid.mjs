import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/+esm";

mermaid.initialize({ startOnLoad: false });
const diagrams = [...document.querySelectorAll(".mermaid")];
for (const [index, sourceNode] of diagrams.entries()) {
  const { svg, bindFunctions } = await mermaid.render(
    `stanag4609-architecture-${index}`,
    sourceNode.textContent.trim(),
  );
  const rendered = document.createElement("div");
  rendered.className = "architecture-diagram";
  rendered.innerHTML = svg;
  sourceNode.replaceWith(rendered);
  bindFunctions?.(rendered);
}
