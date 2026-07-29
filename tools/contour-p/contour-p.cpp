// contour-p: standalone equivalent of `gdal_contour -q -p -amin amin -amax amax
// -fl <levels...> -f FlatGeobuf in.tif out.fgb`, built from GDAL's own
// marching_squares headers (fetched at the pinned GDAL commit by the Dockerfile)
// with patches/gdal-polygon-ring-appender-quadratic.patch applied: polygon
// ring attachment is O(rings^2 x vertices) upstream (OSGeo/gdal#1750, #2241 --
// unfixed post-#2908), which turns marsh-coast depare stems into 60-90 min
// gdal_contour runs; the patched appender is near-linear (85x on a marsh
// window, exact-geometry-equivalent output). Wiring mirrors alg/contour.cpp's
// fixed-levels polygon branch, including its writer min/max level snapping.
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <memory>
#include <cmath>
#include <vector>

#include "gdal.h"
#include "gdal_priv.h"
#include "ogr_api.h"
#include "ogr_geometry.h"
#include "ogr_srs_api.h"
#include "cpl_conv.h"

#include "ms/point.h"
#include "ms/utility.h"
#include "ms/square.h"
#include "ms/level_generator.h"
#include "ms/segment_merger.h"
#include "ms/polygon_ring_appender.h"  // patched in place by the Dockerfile
#include "ms/contour_generator.h"

struct FGBWriter
{
    OGRLayerH layer;
    double gt[6];
    int fMin, fMax;
    double previousLevel_, currentLevel_;
    OGRMultiPolygon *multi = nullptr;
    OGRPolygon *part = nullptr;

    FGBWriter(OGRLayerH l, const double *geot, double minLevel)
        : layer(l), previousLevel_(0), currentLevel_(minLevel)
    {
        for (int i = 0; i < 6; i++) gt[i] = geot[i];
        OGRFeatureDefnH d = OGR_L_GetLayerDefn(layer);
        fMin = OGR_FD_GetFieldIndex(d, "amin");
        fMax = OGR_FD_GetFieldIndex(d, "amax");
    }

    OGRLinearRing *ring(const marching_squares::LineString &ls) const
    {
        auto *r = new OGRLinearRing();
        r->setNumPoints(int(ls.size()));
        int i = 0;
        for (const auto &p : ls)
        {
            const double x = gt[0] + gt[1] * p.x + gt[2] * p.y;
            const double y = gt[3] + gt[4] * p.x + gt[5] * p.y;
            r->setPoint(i++, x, y);
        }
        return r;
    }

    void startPolygon(double level)
    {
        previousLevel_ = currentLevel_;
        multi = new OGRMultiPolygon();
        currentLevel_ = level;
        part = nullptr;
    }

    void addPart(const marching_squares::LineString &ls)
    {
        if (part) multi->addGeometryDirectly(part);
        part = new OGRPolygon();
        part->addRingDirectly(ring(ls));
    }

    void addInteriorRing(const marching_squares::LineString &ls)
    {
        part->addRingDirectly(ring(ls));
    }

    void endPolygon()
    {
        if (part) multi->addGeometryDirectly(part);
        if (multi->getNumGeometries() > 0)
        {
            OGRFeatureDefnH d = OGR_L_GetLayerDefn(layer);
            OGRFeatureH f = OGR_F_Create(d);
            if (fMin >= 0) OGR_F_SetFieldDouble(f, fMin, previousLevel_);
            if (fMax >= 0) OGR_F_SetFieldDouble(f, fMax, currentLevel_);
            OGR_F_SetGeometryDirectly(f, OGRGeometry::ToHandle(multi));
            multi = nullptr;
            if (OGR_L_CreateFeature(layer, f) != OGRERR_NONE)
            {
                fprintf(stderr, "feature write failed\n");
                exit(3);
            }
            OGR_F_Destroy(f);
        }
        if (multi) { delete multi; multi = nullptr; }
        part = nullptr;
    }
};

int main(int argc, char **argv)
{
    if (argc < 4)
    {
        fprintf(stderr, "usage: %s in.tif out.fgb level [level...]\n", argv[0]);
        return 2;
    }
    GDALAllRegister();
    GDALDatasetH in = GDALOpen(argv[1], GA_ReadOnly);
    if (!in) return 2;
    GDALRasterBandH band = GDALGetRasterBand(in, 1);
    double gt[6];
    GDALGetGeoTransform(in, gt);
    int hasNoData = FALSE;
    const double noData = GDALGetRasterNoDataValue(band, &hasNoData);
    // metadata-first like alg/contour.cpp: only scan the raster when no
    // precomputed statistics exist (the exact scan re-reads the whole file).
    double adfMinMax[2];
    int okMin = FALSE, okMax = FALSE;
    adfMinMax[0] = GDALGetRasterMinimum(band, &okMin);
    adfMinMax[1] = GDALGetRasterMaximum(band, &okMax);
    if (!okMin || !okMax)
        GDALComputeRasterMinMax(band, FALSE, adfMinMax);

    std::vector<double> levels;
    for (int i = 3; i < argc; i++) levels.push_back(CPLAtof(argv[i]));
    std::sort(levels.begin(), levels.end());

    double dfMinimum = adfMinMax[0], dfMaximum = adfMinMax[1];
    // verbatim from alg/contour.cpp (v3.13.1) polygonize fixed-levels setup:
    // snap writer min/max to the requested levels bracketing the data range.
    if (!levels.empty())
    {
        if (levels[0] < dfMinimum)
        {
            for (size_t i = 1; i < levels.size(); ++i)
            {
                if (levels[i] >= dfMinimum)
                {
                    dfMinimum = levels[i - 1];
                    break;
                }
            }
        }
        const double maxLevel{levels.back()};
        if (maxLevel > dfMaximum)
        {
            for (size_t i = levels.size() - 1; i > 0; --i)
            {
                if (levels[i] <= dfMaximum)
                {
                    dfMaximum = levels[i + 1];
                    break;
                }
            }
        }
        if (maxLevel == dfMaximum)
        {
            dfMaximum = std::nextafter(
                dfMaximum, std::numeric_limits<double>::infinity());
        }
    }

    GDALDriverH drv = GDALGetDriverByName("FlatGeobuf");
    GDALDatasetH out = GDALCreate(drv, argv[2], 0, 0, 0, GDT_Unknown, nullptr);
    OGRSpatialReferenceH srs = OSRClone(GDALGetSpatialRef(in));
    OGRLayerH layer = GDALDatasetCreateLayer(out, "contour", srs, wkbMultiPolygon, nullptr);
    OGRFieldDefnH fmin = OGR_Fld_Create("amin", OFTReal);
    OGRFieldDefnH fmax = OGR_Fld_Create("amax", OFTReal);
    OGR_L_CreateField(layer, fmin, FALSE);
    OGR_L_CreateField(layer, fmax, FALSE);

    using namespace marching_squares;
    {
        FGBWriter w(layer, gt, dfMinimum);
        typedef PolygonRingAppender<FGBWriter> RingAppender;
        RingAppender appender(w);
        FixedLevelRangeIterator lv(
            &levels[0], levels.size(),
            -std::numeric_limits<double>::infinity(), dfMaximum);
        SegmentMerger<RingAppender, FixedLevelRangeIterator> writer(appender, lv, true);
        std::vector<int> skip;
        skip.push_back(0);
        skip.push_back(static_cast<int>(lv.levelsCount()));
        writer.setSkipLevels(skip);
        ContourGeneratorFromRaster<decltype(writer), FixedLevelRangeIterator>
            cg(band, hasNoData != FALSE, noData, writer, lv);
        if (!cg.process(nullptr, nullptr))
        {
            fprintf(stderr, "contour generation failed\n");
            return 3;
        }
        // appender + writer flush on scope exit
    }
    GDALClose(out);
    GDALClose(in);
    return 0;
}
