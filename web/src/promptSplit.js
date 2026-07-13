/**
 * Map the composer's single prompt textarea onto the backend's title +
 * description. The first line is the title (Task.new requires one); everything
 * after it is the description, which stays optional (null when absent).
 */
export function splitPrompt(prompt) {
  if (!prompt || typeof prompt !== "string") return { title: "", description: null };

  const lines = prompt.split("\n");
  const firstIdx = lines.findIndex((l) => l.trim().length > 0);
  if (firstIdx === -1) return { title: "", description: null };

  const title = lines[firstIdx].trim();
  const body = lines.slice(firstIdx + 1).join("\n").trim();
  return { title, description: body || null };
}
