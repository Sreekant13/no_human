// Resolve hook that swaps the real `electron` for a stub, so main.mjs — the
// wiring where every blocking defect in rounds 8-11 lived — can be driven under
// `node --test`. Claiming it was untestable is what left those bugs uncovered.
export function resolve(specifier, context, next) {
  if (specifier === "electron") {
    return { url: new URL("./electronStub.mjs", import.meta.url).href,
             shortCircuit: true };
  }
  return next(specifier, context);
}
