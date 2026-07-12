export function truncateMiddle(str, max) {
  if (max < 1) return "";
  if (str.length <= max) return str;
  const budget = max - 1;
  const headLen = Math.ceil(budget / 2);
  const tailLen = Math.floor(budget / 2);
  const head = str.slice(0, headLen);
  const tail = tailLen === 0 ? "" : str.slice(str.length - tailLen);
  return `${head}…${tail}`;
}
