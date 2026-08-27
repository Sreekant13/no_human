// A native <datalist> only surfaces options whose `value` starts with the text
// in the paired <input>. So an absolute /Users/you/Downloads option never
// matches a ~/-relative input like "~/Dow" — no completion ever appears. The
// fix is to build each option value in the SAME shape as what the user typed:
// keep everything up to and including the last "/" they typed (the dir prefix),
// then append the suggestion's directory name.
//
//   optionValue("~/Dow", "Downloads")        -> "~/Downloads"
//   optionValue("/Users/x/Dow", "Downloads") -> "/Users/x/Downloads"
//   optionValue("~/git/", "svc")             -> "~/git/svc"
//   optionValue("proj", "projects")          -> "projects"   (no slash yet)
//   optionValue("", "git")                   -> "git"
export function optionValue(input, name) {
  const s = input || "";
  const cut = s.lastIndexOf("/");
  const prefix = cut >= 0 ? s.slice(0, cut + 1) : "";
  return prefix + (name || "");
}
