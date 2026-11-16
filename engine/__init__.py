"""
The mapping engine: one catalogue of every TMS field and selector, and a per-client MAPPING (which source value, in
which format, goes into which field) kept in the database instead of in a client file.

    catalog.py    every TMS field (General / Lines / Cargo / Remarks) and every value a mapping can take from
    recipe.py     a recipe (the saved description of one field's text): check it, render it
    engine.py     MappingFlow — a mapping dressed as a client file, which is all helpers/order_builder needs
    store.py      the client's mapping versions in the database: draft, publish, restore, copy
    starters.py   Emvia's and Sabic's client files written as mappings
    parity.py     proof that a mapping gives the same text as the client file it replaces (python -m engine.parity)

Which one a client uses is decided in helpers/flows.py: a published mapping, else the client's own file in clients/.
"""
