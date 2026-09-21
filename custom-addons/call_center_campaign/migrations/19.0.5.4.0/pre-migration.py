def migrate(cr, version):
    """Retire the table-wide design-revision uniqueness constraint.

    It is replaced by a partial unique index scoped to design-request events so
    that campaign-control events may repeat per configuration version.
    """
    if not version:
        return
    cr.execute(
        """
        ALTER TABLE codestra_runtime_integration_outbox
        DROP CONSTRAINT IF EXISTS
            codestra_runtime_integration_outbox_design_revision_unique
        """
    )
