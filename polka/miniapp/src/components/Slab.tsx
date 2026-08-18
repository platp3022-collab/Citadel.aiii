import { motion, useMotionValue, useReducedMotion, useSpring, useTransform } from "motion/react";
import type { PointerEvent, ReactNode } from "react";
import { useMemo } from "react";

type Props = {
  children: ReactNode;
  /** Толщина плиты в пикселях: чем важнее мысль, тем толще блок. */
  depth?: number;
  /** 1-5, освещённость лицевой грани. */
  lit?: number;
  radius?: number;
  className?: string;
  onClick?: () => void;
  /** Разные плиты стоят чуть по-разному, как вещи на настоящей полке. */
  skew?: number;
};

const BASE_X = 6;
const BASE_Y = -9;
const RANGE = 5;

/**
 * Боковая стенка набирается ступенькой из теней: они повторяют скругление
 * лицевой грани, тогда как повёрнутые панели на скруглённых углах расходятся.
 * Свет падает сверху слева, поэтому блок уходит вправо вниз.
 */
function extrusion(depth: number): string {
  const walls: string[] = [];
  for (let step = 1; step <= depth; step += 1) {
    const shade = 26 - Math.round((step / depth) * 14);
    walls.push(`${step}px ${step}px 0 rgb(${shade} ${shade} ${shade + 3})`);
  }
  return [
    "inset 0 1px 0 rgba(255,255,255,0.15)",
    "inset 1px 0 0 rgba(255,255,255,0.05)",
    "inset 0 -1px 0 rgba(0,0,0,0.45)",
    ...walls,
    `${depth + 2}px ${depth + 6}px ${depth + 14}px rgba(0,0,0,0.55)`,
  ].join(", ");
}

export function Slab({
  children,
  depth = 14,
  lit = 2,
  radius = 18,
  className = "",
  onClick,
  skew = 0,
}: Props) {
  const still = useReducedMotion();
  const px = useMotionValue(0);
  const py = useMotionValue(0);
  const spring = { stiffness: 220, damping: 26, mass: 0.6 };
  const rotateX = useSpring(useTransform(py, [-1, 1], [BASE_X + RANGE, BASE_X - RANGE]), spring);
  const rotateY = useSpring(
    useTransform(px, [-1, 1], [BASE_Y + skew - RANGE, BASE_Y + skew + RANGE]),
    spring,
  );
  const shadow = useMemo(() => extrusion(depth), [depth]);

  function track(event: PointerEvent<HTMLDivElement>) {
    if (still) return;
    const box = event.currentTarget.getBoundingClientRect();
    px.set(((event.clientX - box.left) / box.width) * 2 - 1);
    py.set(((event.clientY - box.top) / box.height) * 2 - 1);
  }

  function release() {
    px.set(0);
    py.set(0);
  }

  return (
    <div className="scene" style={{ paddingRight: depth + 4, paddingBottom: depth + 6 }}>
      <motion.div
        className="slab"
        style={still ? { rotateX: BASE_X, rotateY: BASE_Y + skew } : { rotateX, rotateY }}
        whileTap={still ? undefined : { scale: 0.975 }}
        transition={{ duration: 0.24, ease: [0.32, 0.72, 0, 1] }}
        onPointerMove={track}
        onPointerLeave={release}
        onPointerUp={release}
        onPointerCancel={release}
      >
        <div
          className={`slab__front lit-${Math.min(5, Math.max(1, lit))} ${className}`}
          style={{ borderRadius: radius, boxShadow: shadow }}
          onClick={onClick}
          role={onClick ? "button" : undefined}
          tabIndex={onClick ? 0 : undefined}
          onKeyDown={(event) => {
            if (onClick && (event.key === "Enter" || event.key === " ")) {
              event.preventDefault();
              onClick();
            }
          }}
        >
          {children}
        </div>
      </motion.div>
    </div>
  );
}
