"""Archives the EverQuest launcher doesn't track.

Its file check (even a full verify) never restores these, so an upscaled copy
installed over one would stay in EverQuest for good. They're old files the game
appears not to use any more: zone-specific creature archives replaced by the
shared ones, and the pre-revamp Commonlands and Highpass. They're neither
rendered nor installed.

Found 2026-11-15 on the live client: after installing every archive and running
the launcher's full verify, these 124 were the ones left modified.
"""

UNTRACKED = frozenset({
    "airplane_chr.s3d", "akanon_chr.s3d", "arena_chr.s3d", "befallen_chr.s3d", "beholder_chr.s3d",
    "blackburrow_chr.s3d", "burningwood_chr.s3d", "butcher_chr.s3d", "cabeast_chr.s3d",
    "cabwest_chr.s3d", "cauldron_chr.s3d", "cazicthule_chr.s3d", "charasis_chr.s3d", "chardok_chr.s3d",
    "citymist_chr.s3d", "cobaltscar_chr.s3d", "commons.s3d", "commons_chr.s3d", "commons_obj.s3d",
    "crushbone_chr.s3d", "crystal_chr.s3d", "dalnir_chr.s3d", "dreadlands_chr.s3d", "droga_chr.s3d",
    "eastkarana_chr.s3d", "eastwastes_chr.s3d", "ecommons_chr.s3d", "ecommons_obj.s3d",
    "emeraldjungle_chr.s3d", "erudnext_chr.s3d", "erudnint_chr.s3d", "erudsxing_chr.s3d",
    "everfrost_chr.s3d", "fearplane_chr.s3d", "feerrott_chr.s3d", "felwithea_chr.s3d",
    "felwitheb_chr.s3d", "fieldofbone_chr.s3d", "firiona_chr.s3d", "freporte_chr.s3d",
    "freportn_chr.s3d", "freportw_chr.s3d", "frontiermtns_chr.s3d", "frozenshadow_chr.s3d",
    "gfaydark_chr.s3d", "greatdivide_chr.s3d", "grobb_chr.s3d", "growthplane_chr.s3d",
    "gukbottom_chr.s3d", "guktop_chr.s3d", "halas_chr.s3d", "hateplane_chr.s3d", "highkeep_chr.s3d",
    "highpass.s3d", "highpass_chr.s3d", "hole_chr.s3d", "iceclad_chr.s3d", "innothule_chr.s3d",
    "kael_chr.s3d", "kaesora_chr.s3d", "kaladima_chr.s3d", "kaladimb_chr.s3d", "karnor_chr.s3d",
    "kedge_chr.s3d", "kerraridge_chr.s3d", "kithicor_chr.s3d", "kurn_chr.s3d", "lakeofillomen_chr.s3d",
    "lakerathe_chr.s3d", "lavastorm_chr.s3d", "lfaydark_chr.s3d", "mischiefplane_chr.s3d",
    "mistmoore_chr.s3d", "misty_chr.s3d", "najena_chr.s3d", "necropolis_chr.s3d", "nektulos_chr.s3d",
    "neriaka_chr.s3d", "neriakb_chr.s3d", "neriakc_chr.s3d", "northkarana_chr.s3d", "nro_chr.s3d",
    "nurga_chr.s3d", "oasis_chr.s3d", "oggok_chr.s3d", "overthere_chr.s3d", "paineel_chr.s3d",
    "paw_chr.s3d", "permafrost_chr.s3d", "qcat_chr.s3d", "qey2hh1_chr.s3d", "qeynos2_chr.s3d",
    "qeynos_chr.s3d", "qeytoqrg_chr.s3d", "qrg_chr.s3d", "rathemtn_chr.s3d", "rivervale_chr.s3d",
    "runnyeye_chr.s3d", "sebilis_chr.s3d", "sirens_chr.s3d", "skyfire_chr.s3d", "skyshrine_chr.s3d",
    "sleeper_chr.s3d", "soldunga_chr.s3d", "soldungb_chr.s3d", "soltemple_chr.s3d",
    "southkarana_chr.s3d", "sro_chr.s3d", "steamfont_chr.s3d", "stonebrunt_chr.s3d",
    "swampofnohope_chr.s3d", "templeveeshan_chr.s3d", "thurgadina_chr.s3d", "thurgadinb_chr.s3d",
    "timorous_chr.s3d", "tox_chr.s3d", "trakanon_chr.s3d", "unrest_chr.s3d", "veeshan_chr.s3d",
    "velketor_chr.s3d", "wakening_chr.s3d", "warrens_chr.s3d", "warslikswood_chr.s3d",
    "westwastes_chr.s3d",
})
