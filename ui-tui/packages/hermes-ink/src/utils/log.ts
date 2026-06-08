export function logError(error: unknown): void {
  if (!process.env.THOTH_INK_DEBUG_ERRORS) {
    return
  }

  console.error(error)
}
