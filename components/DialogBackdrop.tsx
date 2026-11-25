import { useLayoutEffect, useRef, type ComponentPropsWithoutRef } from "react";

type Props = Pick<ComponentPropsWithoutRef<"dialog">,
  "children" | "className" | "role" | "aria-label" | "aria-labelledby" | "aria-describedby"> & {
  onDismiss: () => void;
};

export default function DialogBackdrop({ children, className = "dialog-backdrop", onDismiss, ...labels }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const dialog = dialogRef.current!;
    const blockBackdropWheel = (event: WheelEvent) => {
      if (event.target === dialog) event.preventDefault();
    };
    dialog.addEventListener("wheel", blockBackdropWheel, { passive: false });
    dialog.showModal();
    dialog.querySelector<HTMLElement>("[data-dialog-autofocus]")?.focus({ preventScroll: true });
    return () => {
      dialog.removeEventListener("wheel", blockBackdropWheel);
      dialog.close();
    };
  }, []);

  return <dialog {...labels} ref={dialogRef} aria-modal="true" className={className}
    onCancel={(event) => { event.preventDefault(); onDismiss(); }}
    onKeyDown={(event) => {
      if (event.key !== "Escape" || event.defaultPrevented || event.nativeEvent.isComposing) return;
      event.preventDefault();
      event.stopPropagation();
      onDismiss();
    }}
    onMouseDown={(event) => { if (event.target === event.currentTarget) onDismiss(); }}>
    {children}
  </dialog>;
}
