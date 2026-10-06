// Build packages/typescript, export ASKHUMAN_BASE_URL and ASKHUMAN_API_KEY, then run this.
import { AskHuman } from "../packages/typescript/dist/index.js";

const human = new AskHuman();
const request = await human.create("May I publish report Q3 revision 4?", {
  kind: "approval",
  context: "The report has passed validation and will be visible to the team.",
});
console.log(`Saved request: ${request.id}. Answer in the human inbox.`);
const answer = await human.wait(request.id);
console.log(answer.approved === true ? "Publication approved." : "Publication rejected.");
