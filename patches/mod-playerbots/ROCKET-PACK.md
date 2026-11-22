# Gunship rocket-pack setup

Patch `0017` fixes automatic Gunship preparation repeatedly whispering
“Equipping” when the core refuses the belt swap during combat. It is a native
bug fix; rebuild worldserver after applying the patch series in numeric order.
Do not hot-apply it to a running server.

The setup trigger now requires an alive, idle member on ICC's friendly deck,
outside a vehicle and a non-melee cast, with Gunship not completed. An equipped
pack needs no more setup. Execution checks those conditions again, so combat
starting after trigger selection cannot start acquisition or equipping. A wipe
still permits preparation before the next pull.

The equip action uses the core's equipment validation and swap, without the
general EquipAction's master whisper. It reports success only when the pack is
actually equipped. A failed validation or swap is not reported as success.
Manual equip commands and other item announcements are unchanged.

The regression harness compiles these production methods from an applied module
checkout against fake game objects. It checks repeated combat attempts, the
trigger-to-action race, completed encounters, failed swaps, successful setup,
both friendly decks, already-equipped packs and acquisition after a wipe:

```sh
cmake -S patches/mod-playerbots/tests -B scratch/rocket-tests -DPLAYERBOTS_SOURCE=/path/to/patched/mod-playerbots
cmake --build scratch/rocket-tests --config Release
ctest --test-dir scratch/rocket-tests -C Release --output-on-failure
```

The patch CI runs this after applying both the local and dojo series. This
harness does not replace a native rebuild or a real Gunship pull.
