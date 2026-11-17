The person's request is the text after `</skill>` at the end of this message. Keep it in mind
the whole time: every step below works toward that one part.

1. Run `stone status` with the bash tool.
2. Do what its NEXT line says. When NEXT shows `<what to make>`, write the person's request
   there, for example `stone new "a wall hook for a 20 mm rod"`. Never run a NEXT line that
   still has `<` and `>` in it.
3. Keep going, one stone command per bash call, until stone says the part is ready. Run every
   command in the foreground and read its output before the next one: never run stone in the
   background, and use only the bash tool (there is no wait or jobs tool).

If bash prints `stone: command not found`, tell the person to install it with this one command,
then stop: `uv tool install git+https://github.com/baney75/smoothstone@@SHA@`. Never install it yourself.
