The person's request is the text after `</skill>` at the end of this message. Keep it in mind
the whole time: every step below works toward that one part.

1. Run `stone status` with the bash tool.
2. Do what its NEXT line says. When NEXT shows `<what to make>`, write the person's request
   there, for example `stone new "a wall hook for a 20 mm rod"`. Never run a NEXT line that
   still has `<` and `>` in it.
3. Keep going, one stone command per bash call, until stone build says the part is ready. Your
   next call is the command after `NEXT:` in stone's last output, exactly as printed, with
   nothing before or after it. Every stone command is done when it prints: stone new only writes
   down the request, and stone build makes the part. Run every command in the foreground and
   use only the bash tool.

If bash prints `stone: command not found`, stone is not installed: stop and tell the person to
follow step 1 of the Gantrel README to install it. Do not install anything yourself.
