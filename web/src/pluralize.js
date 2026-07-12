export function pluralize(count, singular, plural) {
  if (count === 1) return singular;
  return plural || `${singular}s`;
}
