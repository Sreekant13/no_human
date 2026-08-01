// The no_human brand mark: the `nh` monogram — two arches sharing a stroke,
// picking up the lane colours at the feet.
//
// This serves the SAME image file the marketing site serves, rather than a
// hand-authored SVG copy of it. A copy would be a restatement, and
// restatements drift silently: nothing fails when one of two hand-maintained
// versions of a logo stops matching the other. Serving the asset means the
// app cannot disagree with the site about what the brand looks like.
//
// What was here before was a blue badge with three chevrons, from an earlier
// name. It outlived the rebrand and shipped in demo recordings.
export function LegionLogo({ size = 30 }) {
  return (
    <img className="legion-logo" src="/nh-mark-512.png" alt="" aria-hidden="true"
         width={size} height={size} decoding="async" />
  );
}
