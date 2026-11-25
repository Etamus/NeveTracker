import type { SVGAttributes } from "react";

// Adapted from the Orbiting Spheres variant at https://www.shadcnloaders.com/r/loader.json.
export default function OrbitingSpheres({ size = 20, paused = false, className = "", style, ...props }: SVGAttributes<SVGSVGElement> & { size?: number; paused?: boolean }) {
  return <svg {...props} aria-hidden={props["aria-label"] ? undefined : true} viewBox="0 0 32 32" width={size} height={size} className={`orbiting-spheres ${paused ? "orbiting-spheres--paused" : ""} ${className.replace(/\banimate-spin\b/g, "")}`} style={{ width: size, height: size, ...style }}>
    <g className="orbiting-spheres__rotation"><circle className="orbiting-spheres__dot" cx="16" cy="16" r="5.6" /><circle className="orbiting-spheres__dot" cx="16" cy="16" r="5.6" /></g>
  </svg>;
}
